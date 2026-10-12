import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from io import BytesIO
import json
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx
from PIL import Image
from PySide6.QtWidgets import QApplication

from book_translator.api_errors import APIError, http_failure
from book_translator.api_rotation import ApiProfile, ApiRouter, redact
from book_translator.confidence import Assessment, CombinedAssessment
from book_translator.gemini_service import GeminiService, GeminiError
from book_translator.openai_service import OpenAIService
from book_translator.preferences import GenerationOptions
from book_translator.settings import SettingsDialog

RESULT = {"text": "Hello", "confidence": .9, "regions": []}


class ImmediateCancel:
    """No real delays in retry tests; optionally cancel on the first backoff."""
    def __init__(self, cancel_on_wait=False):
        self.cancelled = False
        self.cancel_on_wait = cancel_on_wait
        self.waits = []
    def is_set(self): return self.cancelled
    def wait(self, seconds):
        self.waits.append(seconds)
        self.cancelled = self.cancel_on_wait
        return self.cancelled


class RotationTests(unittest.TestCase):
    def profiles(self):
        return [ApiProfile("gemini", "gemini-model", "secret-one"), ApiProfile("openai", "gpt-4.1-mini", "secret-two")]

    def factory(self, outcomes, calls):
        def build(profile, options):
            def request(*args):
                calls.append(profile.api_key)
                value = outcomes[profile.api_key].pop(0)
                if isinstance(value, Exception): raise value
                return value
            return SimpleNamespace(extract=request, translate=request, extract_and_translate=request)
        return build

    def test_quota_switches_same_page_and_next_page_stays_on_working_api(self):
        router = ApiRouter(self.profiles()); calls = []
        expected = Assessment.from_dict(RESULT)
        factory = self.factory({"secret-one": [APIError("quota", rotate_now=True)], "secret-two": [expected, expected]}, calls)
        for _ in range(2):
            self.assertIs(router.call("extract", (b"image", "image/png"), None, ImmediateCancel(), factory=factory), expected)
        self.assertEqual(calls, ["secret-one", "secret-two", "secret-two"])
        self.assertEqual(router.snapshot(), (1, True))

    def test_failure_limit_and_backoff_before_switch(self):
        router = ApiRouter(self.profiles(), failure_limit=3); calls=[]; cancel=ImmediateCancel()
        failure = APIError("network", retryable=True)
        factory=self.factory({"secret-one": [failure]*3, "secret-two": [RESULT]}, calls)
        self.assertEqual(router.call("translate", ("text", "English"), None, cancel, factory=factory), RESULT)
        self.assertEqual(calls, ["secret-one"]*3+["secret-two"])
        self.assertEqual(cancel.waits, [1,2])

    def test_success_after_retry_does_not_switch(self):
        calls=[]; router=ApiRouter(self.profiles())
        factory=self.factory({"secret-one":[APIError("network",retryable=True),RESULT]},calls)
        messages = []
        router.call("extract", (), None, ImmediateCancel(), notify=messages.append, factory=factory)
        self.assertEqual(len(messages), 3)
        self.assertIn("Retrying API", messages[1])
        self.assertIn("Running", messages[2])
        self.assertEqual(calls,["secret-one"]*2)
        self.assertEqual(router.snapshot(),(0,True))

    def test_exhaustion_is_bounded_and_redacts_all_keys(self):
        calls=[]; router=ApiRouter(self.profiles())
        factory=self.factory({key:[APIError("Bad " + key,rotate_now=True)] for key in ["secret-one","secret-two"]},calls)
        with self.assertRaises(APIError) as error: router.call("extract", (),None,ImmediateCancel(),factory=factory)
        self.assertNotIn("secret-one",str(error.exception)); self.assertNotIn("secret-two",str(error.exception))
        with self.assertRaises(APIError): router.call("extract", (),None,ImmediateCancel(),factory=factory)
        self.assertEqual(len(calls),2)

    def test_rotation_disabled_never_uses_backup(self):
        calls=[]; router=ApiRouter(self.profiles(),enabled=False)
        factory=self.factory({"secret-one":[APIError("quota",rotate_now=True)]},calls)
        with self.assertRaises(APIError): router.call("extract",(),None,ImmediateCancel(),factory=factory)
        self.assertEqual(calls,["secret-one"])

    def test_refusal_and_output_limits_do_not_rotate(self):
        for message in ["Refused", "Output limit"]:
            calls=[]; router=ApiRouter(self.profiles())
            factory=self.factory({"secret-one":[APIError(message)]},calls)
            with self.assertRaisesRegex(APIError,message): router.call("extract",(),None,ImmediateCancel(),factory=factory)
            self.assertEqual(calls,["secret-one"])
            self.assertEqual(router.blocked_until,{})

    def test_cancel_stops_retries_and_late_failures_cannot_switch(self):
        calls=[]; router=ApiRouter(self.profiles()); cancel=ImmediateCancel(True)
        factory=self.factory({"secret-one":[APIError("network",retryable=True)]},calls)
        with self.assertRaisesRegex(APIError,"cancelled"): router.call("extract",(),None,cancel,factory=factory)
        self.assertEqual(calls,["secret-one"])
        cancel=threading.Event()
        def build(profile, options):
            def extract():
                cancel.set()
                raise APIError("quota",rotate_now=True)
            return SimpleNamespace(extract=extract)
        with self.assertRaisesRegex(APIError,"cancelled"): router.call("extract",(),None,cancel,factory=build)
        self.assertEqual(router.blocked_until,{})

    def test_retry_after_cooldown_and_hard_quota(self):
        self.assertEqual(http_failure(429,retry_after="125")["cooldown"],125)
        self.assertIsNone(http_failure(429,"insufficient_quota")["cooldown"])
        calls=[]; router=ApiRouter(self.profiles())
        factory=self.factory({"secret-one":[APIError("limited",rotate_now=True,cooldown=125),RESULT],"secret-two":[APIError("invalid",rotate_now=True)]},calls)
        with patch("book_translator.api_rotation.time.monotonic",return_value=100):
            with self.assertRaises(APIError): router.call("extract",(),None,ImmediateCancel(),factory=factory)
        with patch("book_translator.api_rotation.time.monotonic",return_value=200):
            with self.assertRaises(APIError): router.call("extract",(),None,ImmediateCancel(),factory=factory)
        self.assertEqual(len(calls),2)
        with patch("book_translator.api_rotation.time.monotonic",return_value=226):
            self.assertEqual(router.call("extract",(),None,ImmediateCancel(),factory=factory),RESULT)


class OpenAIServiceTests(unittest.TestCase):
    def client(self, responder):
        return patch.object(OpenAIService, "_client", lambda service: httpx.Client(
            base_url="https://api.openai.com/v1/",headers={"Authorization":f"Bearer {service.api_key}"},transport=httpx.MockTransport(responder)))

    def completed(self, result):
        return httpx.Response(200,json={"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":json.dumps(result)}]}]})

    def test_three_operations_keep_images_instructions_and_confidence_schema(self):
        requests=[]
        def respond(request):
            self.assertEqual(request.url.path,"/v1/responses")
            self.assertEqual(request.headers['Authorization'],"Bearer test-key")
            body=json.loads(request.content); requests.append(body)
            schema=body['text']['format']['schema']
            return self.completed({"extraction":RESULT,"translation":RESULT} if "extraction" in schema['properties'] else RESULT)
        with self.client(respond):
            service=OpenAIService("test-key","gpt-4.1-mini",GenerationOptions(.25,"COMMON","EXTRACT","TRANSLATE"))
            self.assertIsInstance(service.extract(b"image","image/png"),Assessment)
            self.assertIsInstance(service.translate("Hello","Vietnamese",b"image"),Assessment)
            self.assertIsInstance(service.extract_and_translate(b"image","image/png","Vietnamese"),CombinedAssessment)
        self.assertEqual(len(requests),3)
        for i,body in enumerate(requests):
            self.assertFalse(body['store']); self.assertEqual(body['temperature'],.25)
            self.assertTrue(body['text']['format']['strict'])
            self.assertIn('COMMON',body['instructions'])
            self.assertEqual('EXTRACT' in body['instructions'],i in (0,2))
            self.assertEqual('TRANSLATE' in body['instructions'],i in (1,2))
            self.assertTrue(body['input'][0]['content'][1]['image_url'].startswith('data:image/png;base64,'))

    def test_temperature_can_be_omitted_and_connection_uses_sample(self):
        def respond(request):
            body=json.loads(request.content)
            self.assertNotIn('temperature',body)
            self.assertNotIn('USER_PREF',body['instructions'])
            return self.completed(dict(RESULT,text='CONNECTION_OK'))
        with self.client(respond):
            result=OpenAIService('key','gpt-model',GenerationOptions(common='USER_PREF'),False).test_connection()
        self.assertIn('verified',result)

    def test_error_metadata_and_secret_redaction(self):
        for code,error_code in [(429,'insufficient_quota'),(503,'server_error'),(401,'invalid_api_key')]:
            with self.client(lambda request:httpx.Response(code,json={'error':{'message':'Rejected secret-key','code':error_code}},headers={'Retry-After':'125'})):
                with self.assertRaises(APIError) as raised: OpenAIService('secret-key','gpt-model').extract(b'image','image/png')
            self.assertNotIn('secret-key',str(raised.exception))
            self.assertEqual(raised.exception.retryable,code==503)
            self.assertEqual(raised.exception.rotate_now,code in (429,401))

    def test_refusal_incomplete_and_malformed_never_replace_text(self):
        responses=[({'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'no'}]}]},False),
                   ({'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'}},False),
                   ({'status':'completed','output':[]},True)]
        for response,retryable in responses:
            with self.client(lambda request:httpx.Response(200,json=response)):
                with self.assertRaises(APIError) as raised: OpenAIService('key','gpt-model').extract(b'image','image/png')
            self.assertEqual(raised.exception.retryable,retryable)
            self.assertFalse(raised.exception.rotate_now)

    def test_failed_response_classifies_server_and_quota_errors(self):
        for code in ('server_error', 'insufficient_quota'):
            response={'status':'failed','error':{'code':code,'message':'Provider failure'}}
            with self.client(lambda request:httpx.Response(200,json=response)):
                with self.assertRaises(APIError) as raised:
                    OpenAIService('key','gpt-model').extract(b'image','image/png')
            self.assertEqual(raised.exception.retryable,code=='server_error')
            self.assertEqual(raised.exception.rotate_now,code=='insufficient_quota')

    def test_model_discovery_is_account_scoped(self):
        def respond(request):
            self.assertEqual(request.url.path,'/v1/models')
            return httpx.Response(200,json={'data':[{'id':name} for name in ['gpt-4.1-mini','gpt-model','gpt-realtime','text-embedding-3-small']]})
        with self.client(respond): self.assertEqual(OpenAIService('key','gpt-model').list_models(),['gpt-4.1-mini','gpt-model'])


class ProfileSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def test_add_switch_reorder_and_save_profiles_without_writing_keys(self):
        dialog=SettingsDialog(None,'google-key','gemini-model')
        dialog.add_profile(); dialog.provider.setCurrentIndex(1); dialog.key.setText('openai-key')
        dialog.model.setCurrentText('gpt-4.1-mini'); dialog.send_temperature.setChecked(False)
        dialog.move_profile(-1)
        dialog.profile_selector.setCurrentIndex(1)
        self.assertEqual(dialog.key.text(),'google-key')
        dialog.failure_limit.setValue(2)
        with patch('book_translator.settings.save_settings', return_value='') as save:
            dialog.accept()
        self.assertEqual([p.provider for p in dialog.profiles],['openai','gemini'])
        self.assertFalse(dialog.profiles[0].send_temperature)
        self.assertNotIn('openai-key',repr(dialog.profiles))
        self.assertIsInstance(save.call_args.args[0],GenerationOptions)
        self.assertEqual(dialog.failure_limit.value(),2)

    def test_cancel_does_not_change_parent_profiles(self):
        profiles=[ApiProfile('gemini','gemini-model','secret')]
        dialog=SettingsDialog(None,'secret','gemini-model',profiles=profiles)
        dialog.add_profile(); dialog.key.setText('another'); dialog.reject()
        self.assertEqual(len(profiles),1)
        self.assertEqual(profiles[0].api_key,'secret')

class ProviderWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def test_combined_batch_switches_provider_and_keeps_each_page(self):
        import tempfile
        import time
        from pathlib import Path
        from book_translator.ui import MainWindow
        from book_translator.core import BookDocument
        with tempfile.TemporaryDirectory() as folder:
            paths=[]
            for i in range(2):
                path=Path(folder)/f'{i}.png'; Image.new('RGB',(50,80),'white').save(path); paths.append(path)
            window=MainWindow(); window.set_book(BookDocument.open(paths))
            from dataclasses import replace
            window.generation_options=replace(window.generation_options, auto_update_summary=False)
            window.api_profiles=[ApiProfile('gemini','gemini-model','first-key'),ApiProfile('openai','gpt-4.1-mini','next-key')]
            window.thumbnails.selectAll()
            result=CombinedAssessment(Assessment('Source',.95,[]),Assessment('Translation',.9,[]))
            try:
                with patch.object(GeminiService,'extract_and_translate',side_effect=GeminiError('quota',rotate_now=True)) as google, patch.object(OpenAIService,'extract_and_translate',return_value=result) as openai:
                    window.extract_and_translate()
                    deadline=time.monotonic()+4
                    while window._busy and time.monotonic()<deadline:
                        self.app.processEvents(); window._poll_results(); time.sleep(.005)
                    self.assertFalse(window._busy)
                    self.assertEqual(google.call_count,1)
                    self.assertEqual(openai.call_count,2)
                self.assertTrue(all(page.source=='Source' and page.translation=='Translation' for page in window.book.pages))
                self.assertIn('API 2',window.connection_label.text())
                self.assertIn('OpenAI',window.connection_label.text())
                self.assertEqual(window._router.snapshot(),(1,True))
                self.assertEqual(window._verified_connection,('next-key','gpt-4.1-mini'))
            finally:
                window.cancel_request(); window.dirty=False; window.close()

    def test_gemini_http_error_metadata_preserves_retry_after(self):
        from google import genai
        real_client=genai.Client
        def client(**kwargs):
            kwargs['http_options'].httpx_client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(429,headers={'Retry-After':'130'},json={'error':{'code':429,'message':'quota','status':'RESOURCE_EXHAUSTED'}})))
            kwargs['http_options'].async_client_args={'trust_env':False}
            return real_client(**kwargs)
        with patch('book_translator.gemini_service.genai.Client',side_effect=client):
            with self.assertRaises(GeminiError) as raised: GeminiService('key','gemini-model').translate('text','English')
        self.assertTrue(raised.exception.rotate_now)
        self.assertEqual(raised.exception.cooldown,130)
