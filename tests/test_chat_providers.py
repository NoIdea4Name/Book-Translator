import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest, QSignalSpy
from book_translator.chat_panel import ChatPrompt
from book_translator.openai_service import OpenAIService
from book_translator.nvidia_service import NvidiaService
from book_translator.preferences import GenerationOptions


class ChatProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_enter_sends_and_shift_enter_inserts_newline(self):
        editor = ChatPrompt()
        spy = QSignalSpy(editor.send_requested)
        editor.setPlainText('hello')
        editor.moveCursor(editor.textCursor().MoveOperation.End)
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(editor.toPlainText(), 'hello\n')
        self.assertEqual(spy.count(), 0)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.assertEqual(spy.count(), 1)
        self.assertEqual(editor.toPlainText(), 'hello\n')
        editor.close()

    def test_openai_and_nvidia_chat_contract_images_and_usage(self):
        raw = json.dumps({'answer':'Explanation', 'replacement':'Suggested text'})
        cases = [
            (OpenAIService('secret','gpt-test',GenerationOptions(auto_update_summary=False)),
             {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':raw}]}], 'usage':{'input_tokens':5,'output_tokens':8,'total_tokens':13}}),
            (NvidiaService('secret','vendor/model',GenerationOptions(auto_update_summary=False)),
             {'choices':[{'finish_reason':'stop','message':{'content':raw}}], 'usage':{'prompt_tokens':5,'completion_tokens':8,'total_tokens':13}}),
        ]
        for service, response in cases:
            with self.subTest(provider=service.provider_name), patch.object(service, '_request', return_value=response) as call:
                reply = service.chat({'field':'translation'}, [], 'Improve this', [(b'page','image/png'),(b'region','image/png')])
                self.assertEqual(reply.answer,'Explanation')
                self.assertEqual(reply.replacement,'Suggested text')
                self.assertEqual(reply.token_usage.total,13)
                body = call.call_args.kwargs['json']
                content = body['messages'][1]['content'] if 'messages' in body else body['input'][0]['content']
                self.assertEqual(len(content),3)
                self.assertNotIn('secret',json.dumps(body))
