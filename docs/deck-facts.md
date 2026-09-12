# Core 2000 — verified deck facts

Everything here was read **directly from the collection database**, read-only, not inferred
from the `Core 2000 claude.txt` export. The export does not contain field names.

Source: `%APPDATA%\Anki2\User 1\collection.anki2` (schema 18), Anki 26.08.1.

## Identity

| | |
|---|---|
| Notetype name | `Core 2000` |
| Notetype id | `1342706442507` |
| Notes | 1983 |
| Templates | **exactly one**: `Recognition` |
| Deck name | `Core 2000` |
| Deck id | `1657041025403` |
| Cards | 1983 (1 per note) |

## Fields (authoritative)

| ord | name | actual content | M1 role |
|---|---|---|---|
| 0 | `Optimized-Voc-Index` | integer | — |
| 1 | `Vocabulary-Kanji` | JP word, no furigana | NativeTerm |
| 2 | `Vocabulary-Furigana` | JP word + ruby | NativeTerm (display, `furigana`) |
| 3 | `Vocabulary-Kana` | kana | NativeReading |
| 4 | `Vocabulary-English` | EN gloss | **TargetTerm** |
| 5 | `Vocabulary-Audio` | JP mp3 | **TargetAudio** (repurposed in M5) |
| 6 | `Vocabulary-Pos` | part of speech | POS |
| 7 | `Caution` | 1980/1983 empty | Notes |
| 8 | `Expression` | JP sentence | NativeSentence |
| 9 | `Reading` | JP sentence + ruby | NativeSentence (display, `furigana`) |
| 10 | `Sentence-Kana` | kana sentence | — |
| 11 | `Sentence-English` | EN sentence | **TargetSentence** |
| 12 | `Sentence-Clozed` | JP sentence with `（　）` | ClozeText |
| 13 | `Sentence-Audio` | JP mp3 | **TargetSentenceAudio** (repurposed in M5) |
| 14 | `Notes` | **holds `"Core 2000 Step 01 - 001"`** | — |
| 15 | `Core-Index` | **holds a plain integer** | — |
| 16 | `Optimized-Sent-Index` | integer | — |
| 17 | `Frequency` | integer | — |

> ⚠️ **Field names lie.** `Notes` holds the Core index string; `Core-Index` holds a number.
> This deck is its own best argument for never matching on field names.

## Existing `Recognition` template

Front:

```html
<div class="japanese" style="font-size:60px;">{{Vocabulary-Kanji}}</div>

<div style="font-size: 16px; ">{{Vocabulary-Pos}}</div>
<br/><br/><br/><br/><br/>
<div id="example-sentence" class="japanese" style="font-size:40px;">{{Expression}}</div>
```

Back (abridged — also contains dictionary deep-links for daijirin / wisdom2 / goo / EBPocket /
jisho / weblio, and `{{Frequency}}`):

```html
<div>{{Vocabulary-Audio}}{{Sentence-Audio}}</div>
<div class="japanese" style="font-size:60px;">{{furigana:Vocabulary-Furigana}}</div>
<div style="font-size: 14px; ">{{Vocabulary-Pos}}</div>
<hr id=answer>
<div style="font-size: 30px; ">{{Vocabulary-English}}</div>
<div class="japanese" style="font-size: 50px; ">{{furigana:Reading}}</div>
<div style="font-size: 20px; ">{{Sentence-English}}</div>
```

## CSS notes

The notetype CSS declares `@font-face` rules pointing at **media-folder files**:
`_stroke.ttf`, `_HGSKyokashotai.ttf`, `_NotoSansJP-Medium.otf`, plus one remote Google font.
It defines `.card` (white on black, 25px, centred), `.japanese`, `.stroke`, and the platform
classes `.ios-only` / `.mac-only` / `.mobile` / `.mac`.

**Carry this CSS over verbatim** onto the clone. The clone shares the same media folder, so the
font references keep working and generated cards look native immediately.

## Data quirks that shape the code

- `Vocabulary-English` contains HTML noise and sometimes Japanese:
  `skilled,&nbsp;good` · `<!--anki-->cease, stop,&nbsp;let up` ·
  `add, supplement<div>(the same kind of thing)</div>` ·
  `processing,&nbsp;management<div>(unlike 加工, a new thing is not created)</div>` ·
  `be in time, serve (my) purpose<br>なくても〜 do without`
  → a TTS sanitizer is mandatory before M2.
- `Sentence-English` is clean prose by comparison.
- **Duplicate prompts**: 31 English glosses shared by 64 notes
  (兄 / お兄さん / 兄さん all → "older brother"; 何 / どれ → "what, which").
  46 English sentences shared by 93 notes.
  → rationale for putting POS **and** the English sentence on the front.
- Empty audio: 2 notes have empty `Vocabulary-Audio` (危険, 看護師), 1 has empty
  `Sentence-Audio` (看護師) → templates need conditional sections.
- `Caution` is empty on 1980 of 1983 notes; the 3 non-empty values are short notes
  like `not 全然`.
- Tags are mostly empty; `leech` appears on some notes (scheduling-derived — strip it when
  duplicating notes in Mode B, since scheduling is reset anyway).
- Some note indices are missing (442, 970, 1258, …) — deleted notes, harmless.

## Export file format

`Core 2000 claude.txt` header:

```
#separator:tab
#html:true
#notetype column:1
#deck column:2
#tags column:21
```

21 columns on every row: col 1 notetype, col 2 deck, **cols 3–20 = the 18 fields in ord order**,
col 21 tags. The file is **quoted CSV-style** — e.g. the 言う row contains
`"My boss said: ""Let's have a drink."""`. Parse with `csv.reader(..., delimiter='\t')`,
never `str.split('\t')`.
