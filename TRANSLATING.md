# Adding an interface language

The app ships with English (`en`) and Vietnamese (`vi`). Select a language in
Settings, press Save, and restart. This preference is stored on this computer,
separately from projects, API credentials and the book's translation target.

1. Copy `book_translator/locales/en.json` to `book_translator/locales/fr.json`
   (replace `fr` with your language code).
2. Set `name` to a native display name, for example `Français`.
3. Translate the **values** in `messages`. Keep every English key unchanged.
4. Preserve named placeholders and their formatting exactly: `{p0}`, `{count}`,
   `{p0:,}`, `{p1:.0%}`. You can move placeholders to suit the sentence.
   Preserve URLs, keyboard shortcuts, and HTML links. Use `\n` for line breaks.
   `&&` displays a literal ampersand in Qt buttons.
5. Save as UTF-8 JSON. Restart the app; the new pack appears in Settings.
6. Test menus, Settings tabs, status messages, Find/Replace, chat and export dialogs.

Example:

```json
{
  "name": "Français",
  "messages": {
    "Settings": "Paramètres",
    "{count} pages selected": "{count} pages sélectionnées"
  }
}
```

Partial packs work: missing entries fall back to English. Entries with incompatible
placeholders are ignored, preventing formatting failures. Invalid JSON files are
not offered in the language selector. `en.json` is the translation template.

Developer notes: wrap user-facing text in `tr("English text")`, or use named
formatting arguments, such as `tr("{count} pages selected", count=count)`. Add its
English key to both language files. Never translate user content, API model IDs,
internal enum values, filenames, machine-readable responses or AI instructions.
For combo boxes use translated display labels with stable `itemData`, or retain
index-based behavior. `i18n.py` owns loading, validation and English fallback.

The desktop UI is localized. Provider error details, AI-generated explanations,
existing page names, exported document headings and the phone capture website
remain unchanged. They are not interface language-pack keys.
