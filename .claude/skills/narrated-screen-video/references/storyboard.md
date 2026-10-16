# Storyboard format and story design

## Contents
1. File layout
2. Actions reference
3. Targets
4. Story arc that works
5. Pacing rules
6. Stage cards
7. Drafting from the live app
8. Templates by product type
9. Command-line tools

## 1. Layout

```json
{
  "title": "...",
  "stages": { "intro": {...}, "problem": {...}, "outro": {...} },
  "beats": [
    {"chapter": "Overview", "say": "Spoken text.", "cap": "Caption text if different", "do": [ {action}, {action} ]}
  ]
}
```
- `say` is synthesized and burned in as the caption (use `cap` to override, e.g. correct spelling of respelled words).
- `do` actions run in order at the start of the beat; the beat then waits until the audio plus tail has finished. If the actions take longer than the sentence, the beat grows to fit and sync is preserved.
- `{BADGE}` inside an action value is replaced by `--badge` text (e.g. `{"badge": "{BADGE}"}`), `chapter` marks a YouTube chapter.

## 2. Actions

| Action | Meaning |
|---|---|
| `{"stage": "name"}` / `{"stage": null}` | show / hide a full-screen title card (captions are suppressed over cards) |
| `{"reveal": "over"}` / `{"reveal": 3}` | reveal a card's items spread across the sentence / the first 3 |
| `{"tab": "<target>"}` | glide the cursor to a nav target, click it, reset scroll to top |
| `{"scroll": "<target>", "center": true, "ms": 1600}` | smooth-scroll the page so the target sits under the sticky header (or centred) |
| `{"scroll_y": 600}` | scroll to an absolute offset |
| `{"scroll_sheet": "#modalBody"}` | scroll inside an open modal/drawer (CSS selector of its scrolling box) down and back |
| `{"spot": "<target>" or [targets], "label": "Why it matters"}` / `{"spot": null}` | dim everything else, ring the target(s), optional label chip |
| `{"hover": "<target>"}` | cursor glides there (triggers tooltips) |
| `{"sweep": "<target>", "over": true}` | drag the cursor across a chart; `over` stretches it over the whole sentence |
| `{"click": "<target>"}` | glide and click (open a modal, expand a row) |
| `{"hscroll": "<target>", "to": 1, "ms": 2000}` | scroll a wide table sideways (0 = left, 1 = right) |
| `{"key": "Escape"}` | press a key (close a modal) |
| `{"badge": "{BADGE}"}` | corner badge (use for sample data) |
| `{"eval": "document.body.classList.add('dark')"}` | run JS: toggle theme, expand panels, anything app-specific |
| `{"wait": 0.8}` | pause |

`hscroll` is a silent no-op when the table already fits (wide screens): that is fine, not an error.

## 3. Targets

CSS selector, `text:<exact visible text>`, or any prefix you register in `window.V_CONFIG.resolvers` (for example `card:<heading>`, `kpi:<label>`, `nav:<slug>` in the dashboard this was built on). Write them in a small `cfg.js` passed as `--config-js`:

```js
window.V_CONFIG = {
  accent: '#c2573a',
  root: 'main section:not([hidden])',          // search scope for text:/resolvers (the visible tab)
  ready: '.kpi',                                // selector proving data has rendered
  resolvers: {
    card: (want, root) => [...root.querySelectorAll('.card')].find(c => c.querySelector('h3')?.textContent.trim().toLowerCase() === want.toLowerCase()),
    nav:  (want) => document.querySelector(`#nav button[data-s="${want}"]`)
  }
};
```
Resolve against what is **visible now**: after a `tab` action, only that tab's content exists in scope. A missing target prints `! target not found` and the beat carries on - always grep the build log for it.

## 4. Story arc that works

1. **Title card** - name + one-line promise.
2. **The problem** (1-3 cards) - what is invisible or painful today, said in the viewer's words.
3. **What exists and why it falls short** - a list card, items revealed in step with the voice.
4. **The questions nobody can answer** - the product's reason to exist.
5. **The solution card** - 3-4 chips (local, private, zero cost...) then hide it and show the app.
6. **Tour, tab by tab** - for each tab: arrive (tab + one-sentence purpose), 2-4 beats each spotlighting one card and saying what *decision* it supports, one scroll or sideways move so the viewer sees there is more, optionally one modal.
7. **Wrap-up card** - the one command or URL to get started.

## 5. Pacing rules

- 10-30 words per beat; about 1 beat per 8-10 seconds of video; 8 minutes is ~50 beats.
- Never leave more than ~3 s of motionless screen while the voice talks - add a spotlight or a slow scroll.
- Don't fight the voice: movement during a sentence should *illustrate* it, finishing slightly before the sentence ends.
- A modal or dark-theme moment near the end adds visual variety.
- Keep the cursor off the caption bar (the recorder clamps it) and off the sticky header.

## 6. Stage cards (`stages`)

Keys: `mark` (logo tile), `eyebrow`, `title` (`**word**` becomes the accent colour), `sub`, `items` (`{k,v}` rows or `{v}` question rows), `chips`, `cmd` (monospace box), `url`, `align: "center"`. Dark gradient background, readable at 1600x900. Restyle in `overlay.js` (`#v-stage`, `.vs-*`) to match the brand.

## 7. Drafting from the live app

`python scripts/draft_storyboard.py --url URL --out script.json --shots shots/` opens the app, finds the navigation (tabs, sidebar, top nav; pass `--nav "css"` if it guesses wrong), clicks through each item and records for every view: the heading, a KPI row if there is one, each card (kind: card / chart / table), and whether a table scrolls sideways. It writes:
- `script.json` with `tab`, `scroll`, `spot`, `sweep`, `hscroll` actions and real selectors already in place;
- `_context` on each beat: the on-screen text of that block, to write the narration from;
- screenshots (`shots/NN-name.png` and `-full.png`): open them, you are the only one who can judge what matters.

All narration is `TODO:`; `build_video.py` refuses to run until none is left. The tool cannot know *why* a number matters, so do not auto-fill sentences from `_context`: write what question the view answers and what a viewer should do with it. Selectors are generated paths (`#id` where possible, else `tag:nth-of-type` chains) and can break if the app changes: prefer `text:` or an id you add yourself for anything important.

## 8. Templates by product type (`assets/templates/`)

| File | Shape |
|---|---|
| `dashboard.json` | problem -> what exists -> questions -> solution -> tabs (KPI, chart, wide table) -> wrap-up |
| `saas-onboarding.json` | before -> sign-in/first screen -> core step -> result; use `--storage-state` + `--blur-auto` |
| `api-docs.json` | auth -> first request -> response -> reference; copy real responses |
| `cli-tool.json` | install -> first run -> a useful flag; uses `terminal-replay.html` |
| `release-notes.json` | headline -> 2-4 changes shown live -> fixes -> upgrade; 60-90 s |

Each beat's `say` says what that sentence has to accomplish. Replace every `TODO`, delete beats you don't need, add more per tab.

## 9. Command-line tools

`assets/terminal-replay.html` is a scripted terminal page. Serve the `assets` folder (`python3 -m http.server 8790 -d assets`), record `http://127.0.0.1:8790/terminal-replay.html` with `--config-js` containing `window.V_CONFIG = { ready: '#term', root: 'body' };`, and drive it with `eval` actions:

```json
{"eval": "T.run([['cmd','acme init'],['out',['Created acme.config.json','{g}Ready.{/}']],['wait',600]])"}
```
Steps: `cmd` (types, then Enter), `out` (lines, optional `{delay}`), `comment`, `wait`, `clear`. Colours `{g}{r}{y}{b}{d}...{/}`. Run the real command first and paste what it really printed - an invented output is a lie in a product video. Keep commands short and output under ~8 lines per beat. A working example: `assets/examples/cli-demo.json`.
