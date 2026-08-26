# Setting up WikiPeople on a wiki

WikiPeople is currently a **personal script**: you install it for yourself, on one wiki, and only you
see it. Everything below is something you do in your own user space, with no special rights and
nobody else's permission.

Toolforge-side operation is covered separately in the [operations runbook](operations.md).

## The three pages in your user space

All three live under your own user name, on whichever wiki you are installing on:

| Page | What it is | Required? |
| --- | --- | --- |
| `User:YOU/wikipeople.js` | The script | Yes |
| `User:YOU/wikipeople.css` | Its styles | Yes |
| `User:YOU/wikipeople-config.json` | Your settings for this wiki | No |

Substitute your wiki's own user-namespace name where it differs — `Utilisateur:` on the French
Wikipedia, for example. The script resolves that itself, so the three pages always sit together
whatever the wiki calls the namespace.

Then load the first two from `User:YOU/common.js`:

```javascript
importScript( 'User:YOU/wikipeople.js' );
importStylesheet( 'User:YOU/wikipeople.css' );
```

The configuration page is **not** imported. The script looks it up by name on its own.

## Before you start: is the wiki covered?

WikiWho publishes provenance data for around seventy Wikipedia language editions, from Afrikaans to
Chinese, including Simple English. Commons, Wikidata, Wiktionary, and Wikisource are not covered
and cannot be — there is no surviving-token provenance for them.

On a wiki that is not covered, the API answers `404` and the script renders nothing. Installing it
there does no harm, but it does nothing either.

## Creating the configuration page

Everything works without it. Its one real job is supplying the two local page titles the script
cannot guess: your wiki's editing help and its sandbox. Without them, the "to get started, read …
or practise in …" sentence in the history box is simply left out.

1. Pick the file for your wiki from [`config/`](../config) in this repository — currently
   [`enwiki.json`](../config/enwiki.json) and [`frwiki.json`](../config/frwiki.json).
2. Create `User:YOU/wikipeople-config.json` on that wiki and paste it in.
3. For a wiki with no published default yet, copy either file and replace the two titles with your
   wiki's own, including their namespace, exactly as they appear locally.
4. Save. MediaWiki treats `.json` subpages as JSON, validates them, and refuses to save invalid
   JSON — so a typo cannot reach the script. It reformats with tab indentation; that is expected.
5. Reload an article **in a new tab**. The script caches the configuration in `sessionStorage`, so
   an already-open tab may still be using the previous version.

Both files state **every** option, at its default, and open with a `"//"` block naming what each
one accepts. The gadget reads six keys and ignores everything else, `"//"` included, so that block
costs nothing and means the page you are editing on the wiki explains itself — which matters,
because JSON pages cannot carry comments and nobody reads a repository from a wiki edit box.

Keep the options you do not change. A page listing all six shows the next person what exists.

### English Wikipedia — [`config/enwiki.json`](../config/enwiki.json)

```json
{
	"//": {
		"note": "Everything in this block is documentation for whoever edits this page. The gadget reads the six options below it, checks each value against the list given here, and ignores every other key — including this one.",
		"documentation": "https://github.com/schiste/wikifame/blob/main/docs/onwiki-setup.md",
		"enabled": "true or false. Default: true. false stops the gadget on this wiki.",
		"showHistoryIntro": "\"anonymous\", \"always\" or \"never\". Default: \"anonymous\", which shows the history box to logged-out readers only.",
		"editHelpPage": "A local page title, or null. Default: null. Used only together with sandboxPage.",
		"sandboxPage": "A local page title, or null. Default: null. Used only together with editHelpPage.",
		"historyIntroPage": "A local page title, or null. Default: null. Its wikitext replaces the built-in history box text.",
		"messages": "An object mapping a message key to its replacement text. Default: {}. The keys are listed in the documentation above."
	},
	"enabled": true,
	"showHistoryIntro": "anonymous",
	"editHelpPage": "Help:Editing",
	"sandboxPage": "Wikipedia:Sandbox",
	"historyIntroPage": null,
	"messages": {}
}
```

### French Wikipedia — [`config/frwiki.json`](../config/frwiki.json)

```json
{
	"//": {
		"note": "Everything in this block is documentation for whoever edits this page. The gadget reads the six options below it, checks each value against the list given here, and ignores every other key — including this one.",
		"documentation": "https://github.com/schiste/wikifame/blob/main/docs/onwiki-setup.md",
		"enabled": "true or false. Default: true. false stops the gadget on this wiki.",
		"showHistoryIntro": "\"anonymous\", \"always\" or \"never\". Default: \"anonymous\", which shows the history box to logged-out readers only.",
		"editHelpPage": "A local page title, or null. Default: null. Used only together with sandboxPage.",
		"sandboxPage": "A local page title, or null. Default: null. Used only together with editHelpPage.",
		"historyIntroPage": "A local page title, or null. Default: null. Its wikitext replaces the built-in history box text.",
		"messages": "An object mapping a message key to its replacement text. Default: {}. The keys are listed in the documentation above."
	},
	"enabled": true,
	"showHistoryIntro": "anonymous",
	"editHelpPage": "Aide:Comment modifier une page",
	"sandboxPage": "Wikipédia:Bac à sable",
	"historyIntroPage": null,
	"messages": {}
}
```

If you work out the right titles for a wiki that has no default yet, please send them back as a
pull request so the next person on that wiki does not have to.

## Fields

| Key | Accepts | Default | Effect |
| --- | --- | --- | --- |
| `enabled` | `true`, `false` | `true` | `false` switches the script off on this wiki. It stops before rendering anything. |
| `showHistoryIntro` | `"anonymous"`, `"always"`, `"never"` | `"anonymous"` | Who gets the explanatory box on page-history views. See [Who sees the history box](#who-sees-the-history-box). The attribution sentence on articles is not affected. |
| `editHelpPage` | a local page title, or `null` | `null` | Local title of the editing help page. |
| `sandboxPage` | a local page title, or `null` | `null` | Local title of the sandbox. |
| `historyIntroPage` | a local page title, or `null` | `null` | Title of a wikitext page whose content replaces the history box text. See [Rich content](#rich-content-images-video-anything-wikitext-can-do). |
| `messages` | an object of message key to text | `{}` | Overrides individual interface strings by key. See the warning below. |

Unknown keys are ignored, so a future option can be added without breaking existing pages.

A value outside the "Accepts" column is ignored the same way, and the default applies. That covers
the mistakes JSON invites: `"false"` as a string, `0`, or `null` are not values `enabled` accepts,
so none of them switches the script off — write a real JSON `false`. The same goes for a misspelt
`"anonymou"`, which is not a fourth state but simply no instruction at all. Nothing warns you, so
copy the values above rather than typing them.

`showHistoryIntro` was a boolean before it grew a third state. `true` and `false` are still
understood, as `"always"` and `"never"`, so a page written against an older version of the script
keeps doing what it said.

`editHelpPage` and `sandboxPage` work as a pair. The help sentence appears only when **both** are
set; setting just one leaves it out entirely.

### Who sees the history box

The box explains what a page history is: worth the space for someone who has never seen one, noise
for someone who came to the page to read it. So the default, `"anonymous"`, shows it to logged-out
readers only.

Being logged in is not the same as knowing the wiki — but it is the only signal the script has, and
the reader it gets wrong is exactly the one who can open this page and write `"always"`.

**While WikiPeople is a personal script, this means you do not see the box yourself.** Your
configuration page lives in your user space and is only read when you are logged in, which is the
one case the default hides it. Set `"always"` while you are working on the box, or to keep it for
yourself; `"never"` turns it off for everybody. The three states only really pay off
[later](#later-becoming-a-site-wide-gadget), when the configuration stops being personal and one
setting covers every reader of a wiki.

## Where the wording comes from

The script carries its own text in English and French, and picks which to use from **your interface
language** (`wgUserLanguage`), not from the wiki. Four layers apply in order, each overwriting the
one before:

1. built-in English — always applied, the floor;
2. built-in text for your base language, e.g. `fr` for a reader set to `fr-ca`;
3. built-in text for your exact language code;
4. whatever `messages` in your configuration page says.

So on the French Wikipedia a reader with a French interface sees French, and a reader with a German
interface sees English — because German is not built in yet, not because of any configuration.

### Leave `messages` empty

**Recommended: omit it, or leave it as `{}`.** Layers 1–3 follow the reader's language; layer 4
does not. An override replaces that string for **every** language, so text written to improve the
French wording also replaces the English one.

That matters less for a personal script, where you are the only reader, than it will when this
becomes a site-wide gadget. But the better fix in almost every case is to add the wording to the
script's own message table, where it is language-aware and helps every wiki at once.

Available keys, with the built-in English text:

| Key | Default |
| --- | --- |
| `wikipeople-summary-prefix` | `Article written by ` |
| `wikipeople-summary-prefix-edits` | `Article most edited by ` |
| `wikipeople-people` | `{{PLURAL:$1|$1 person|$1 people}}` |
| `wikipeople-others` | `{{PLURAL:$1|$1 other person|$1 other people}}` |
| `wikipeople-at-least` | `at least $1` |
| `wikipeople-pending` | `Analysing contributions…` |
| `wikipeople-many-people` | `many people` |
| `wikipeople-user-title` | `View the user page of $1` |
| `wikipeople-share` | `$1 of the currently visible tokens` |
| `wikipeople-share-edits` | `$1 of the edits to this page` |
| `wikipeople-history-title` | `View the full page history` |
| `wikipeople-tooltip` | `Main authors of the text according to WikiWho.` |
| `wikipeople-tooltip-edits` | `Accounts that edited this page most, from its history. The text itself could not be analysed.` |
| `wikipeople-computed` | `Data computed on $1.` |
| `wikipeople-history-intro` | `Each line is one version of the article, showing who changed it.` |
| `wikipeople-history-help` | `To get started, read $1 or practise in $2.` |
| `wikipeople-history-help-label` | `the editing help` |
| `wikipeople-history-sandbox-label` | `the sandbox` |
| `wikipeople-history-edit` | `You can also $1.` |
| `wikipeople-history-edit-label` | `edit this article directly` |

The three `-edits` keys are used only when the text itself could not be analysed and the names come
from the page history instead — who edited most, rather than who wrote what you are reading. They
are worded as a weaker claim on purpose. If you override them, keep them weaker than their
counterparts: the same names under `wikipeople-summary-prefix` would credit people for text they may
never have written.

If you do override something:

- Keep every `$1` and `$2` placeholder. They are replaced by real links and numbers; a message that
  drops its placeholder silently loses that link.
- `{{PLURAL:$1|…}}` is supported and should be kept, with as many forms as the language needs.
- Values are inserted as text, never as HTML. Wikitext markup will appear literally.
- Only string values are applied; anything else is ignored.

## Rich content: images, video, anything wikitext can do

The `messages` object handles words. When you want more than words in the history box — a diagram,
a screenshot of the history page with callouts, a short Commons video, a template — write a
**wikitext page** and point at it:

```json
"historyIntroPage": "User:YOU/wikipeople-history"
```

Then create `User:YOU/wikipeople-history` and write ordinary wikitext:

```wikitext
[[File:Wikipedia history page annotated.png|thumb|right|300px|Each line is one version.]]
Every line below is one version of this article, and the name on it is the person who
made that change. Nothing here is permanent: you can add to it too.
```

MediaWiki parses and sanitises that page, and the script inserts the result. Images, galleries,
Commons video, templates, tables, and formatting all work, because none of it is being handled by
this script — the wiki does the work, and you preview and revert it like any other page.

### What you should know before using it

- **It replaces, it does not add.** When the page exists, its content takes the place of the
  built-in explanation *and* the editing-help sentence. `editHelpPage` and `sandboxPage` stop
  affecting the box, so re-add those links in your wikitext if you still want them.
- **The "you can also edit this article" line stays**, always, below your content. It is built by
  the script on purpose: your page is parsed on its own, so it has no idea which article the reader
  is looking at. `{{FULLPAGENAME}}` in your wikitext would resolve to the *introduction page*, and
  the link would offer to edit the wrong page. Same for `{{PAGENAME}}` and friends.
- **One language per page.** The script tries `…/fr-ca`, then `…/fr`, then the bare title, using
  your interface language. So `User:YOU/wikipeople-history/fr` serves French readers and
  `User:YOU/wikipeople-history` catches everyone else. This is the piece `messages` gets wrong, and
  the reason to prefer this route for anything longer than a phrase.
- **Weight is on you.** This box renders on every history view. A large image or an autoplaying
  video would be paid for every time. The script sets images to load lazily and stops video from
  autoplaying or preloading, but it cannot make a 4 MB PNG small.
- **Video needs TimedMediaHandler**, which most Wikipedias have. Where it is missing you get a
  plain player rather than the enhanced one. Nothing breaks.
- **A missing page is not an error.** If the page does not exist, is deleted, or the wiki is
  unreachable, the built-in wording renders instead. The result — including "there is no such
  page" — is cached for 24 hours, so create the page *before* pointing at it, or expect up to a
  day's delay in a tab you have already opened.

### Showing the real number of authors

Your page is parsed once and reused for every article, so it cannot contain this article's
contributor count. Declare a slot instead, and the script fills it in:

```wikitext
Cet article a été écrit par <span class="wikipeople-count">des dizaines de personnes</span>.

<span class="wikipeople-number">plusieurs centaines</span> de personnes y ont contribué.
```

| Class | Becomes |
| --- | --- |
| `wikipeople-count` | The full localised phrase — `1 234 personnes`, correctly pluralised, prefixed with `au moins` when the count is a lower bound. |
| `wikipeople-number` | Just the formatted number — `1 234` — for when you write the sentence yourself. |

Use as many of each as you like; they all get the same value.

- **Whatever you write inside the element is the fallback.** It stays exactly as written if the
  article has no result yet, if the wiki is not covered, or if the API is unreachable. So write
  something that reads well on its own — `des dizaines de personnes`, not `…`, and never leave it
  empty.
- **No slot means no request.** A page without either class costs nothing on a history view, which
  is why this is opt-in rather than always on.
- **It does not wait.** The box renders first and the number lands a moment later. On an article
  whose result is still being computed the script does not retry and does not rewrite the box —
  your fallback wording simply stays.
- **It is one API call, not two.** The count shares the session cache with the article-view
  sentence, keyed by page, so reading an article and then opening its history costs one request.

### JavaScript

Not through the configuration page — a `.json` page that might contain code is a page nobody can
review by reading it. The script fires two hooks instead:

```javascript
mw.hook( 'wikipeople.history' ).add( function ( box, wikiConfig ) {
	// box is the rendered <div>, already in the page.
} );

mw.hook( 'wikipeople.summary' ).add( function ( summary, data ) {
	// data.contributors, data.distinct_contributors, data.computed_at…
} );
```

Put that in your own `common.js`, after the `importScript` line. This gives you the full DOM and
the full language, which is strictly more than a configuration page could ever offer, and it keeps
the JSON to things that can be validated.

## When nothing renders

The script is deliberately silent — it never shows an error to a reader. Work through these in
order:

| Symptom | Likely cause |
| --- | --- |
| Nothing on any article, this wiki only | The wiki is not covered by WikiWho, or `enabled` is `false`. |
| Nothing on one article, others fine | No result computed yet. The first request queues the work; come back later. Normal for a page nobody has viewed with the script before. |
| Nothing anywhere, on every wiki | The script is not loading. Check the `importScript` line in your `common.js`, and the browser console. |
| No box on page-history views, but articles are fine | `showHistoryIntro` is at its default `"anonymous"` and you are logged in. Set `"always"`. |
| The sentence shows but the help sentence does not | `editHelpPage` and `sandboxPage` are not both set, `showHistoryIntro` is `"never"`, or `historyIntroPage` is set and has replaced it. |
| Configuration edits have no effect | Stale `sessionStorage`; open a new tab. Or a key is misspelled, or its value is not one the option accepts — both are ignored silently. |
| `historyIntroPage` is set but the built-in text still shows | The page does not exist under any of the three titles tried, or its absence is still cached. Create it, then open a new tab. |
| Custom content renders but looks wrong | It is your wikitext, parsed as usual. Preview the page on its own; what you see there is what the box gets. Oversized media is constrained by `wikipeople.css`, not fixed. |
| The author count stays on its fallback wording | No result for this article yet — open the article itself once, wait, then come back. Also check the class name: `wikipeople-count`, on an element, not a template parameter. |

The attribution sentence appears on normal article views only: not on diffs, not on old revisions,
not outside the main namespace.

For a deeper look, open the browser console. Initialisation failures are logged through
`mw.log.warn` with a `WikiPeople:` prefix.

## Later: becoming a site-wide gadget

Once a community adopts WikiPeople for all its readers, the configuration stops being personal and
moves to `MediaWiki:Wikipeople-config.json` on that wiki — same fields, same file, one copy shared by
everyone, editable by interface administrators. The files in [`config/`](../config) become the
starting point for that page instead of for a personal one.

That step needs a community discussion first; nothing in the design substitutes for asking. Until
then, user space keeps the prototype installable by anyone, with no rights and no gatekeeper.

## See also

- [Operations runbook](operations.md) — the Toolforge side, including how to pin a wiki for
  prewarming.
- [Architecture](architecture.md) — how a wiki is resolved and what gets stored.
- [ADR-0003](decisions/0003-universal-wiki-support.md) — why configuration lives on-wiki rather
  than in the service.
- [ADR-0004](decisions/0004-on-wiki-extensibility.md) — why rich content is a wikitext page and
  JavaScript is a hook, rather than either living in the JSON.
