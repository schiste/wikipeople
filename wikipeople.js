/* global mw */
/**
 * WikiPeople — names the people who wrote the text you are reading.
 *
 * The script is wiki-agnostic: it reports its own wiki through wgDBname and lets the
 * API decide whether that wiki is served. An unsupported Wikipedia answers 404, the
 * fetch rejects, and nothing is rendered — so this same file can ship on every
 * Wikipedia while the backend enables wikis one at a time. The refusal is remembered
 * for a day, so a reader whose script manager carries this file onto Commons or
 * Wikidata asks once instead of on every page.
 *
 * Wording and help links come from the reader's own User:<name>/wikipeople-config.json
 * on the local wiki, alongside the script itself. While WikiPeople is a personal script
 * the reader and the installer are the same person, so no interface-admin rights are
 * needed and each wiki gets its own copy. The page is optional: without it the
 * built-in defaults apply. Defaults per wiki are published in the repository.
 *
 * When this becomes a site-wide gadget the page moves to MediaWiki:Wikipeople-config.json
 * and only CONFIG_PAGE_SUFFIX and configPage() change.
 *
 * Every option, its default, and the values it accepts are DEFAULT_CONFIG and ALLOWED_VALUES
 * below. The published pages in config/ and the field table in docs/onwiki-setup.md say the
 * same thing to the person editing the page, and a test holds the three together.
 *
 * Two extension points exist so that nobody has to fork this file:
 *
 *   historyIntroPage  a wikitext page whose parsed HTML replaces the built-in history
 *                     introduction. Images, galleries, Commons video and templates all
 *                     work, because MediaWiki does the parsing and the sanitising.
 *   count slots       an element of class wikipeople-count or wikipeople-number in that page
 *                     receives this article's contributor count, which the page itself
 *                     cannot hold: it is parsed once and cached for every article.
 *   mw.hook           'wikipeople.history' and 'wikipeople.summary' fire with the rendered
 *                     element, so arbitrary JavaScript belongs in the reader's own
 *                     common.js rather than in a configuration page.
 */
( function () {
	'use strict';

	var ARTICLE_SUMMARY_ID = 'wikipeople-summary';
	var HISTORY_INTRO_ID = 'wikipeople-history-intro';
	var CACHE_VERSION = 'v2';
	var TOOLFORGE_API_BASE = 'https://wikipeople.toolforge.org';
	var CONFIG_PAGE_SUFFIX = '/wikipeople-config.json';
	var REQUEST_TIMEOUT_MS = 8000;
	// Deliberately short, and deliberately the same number the API puts in its max-age.
	// This cache exists to spare a second request when a reader moves between an article
	// and its history, which happens in seconds; holding an answer for a day instead
	// meant a policy change on the server was invisible here for a day, because a reader
	// with the answer already in hand never asks again and so never finds out it changed.
	// Past this window the browser's own cache still answers most requests with a 304.
	var CLIENT_CACHE_MAX_AGE_MS = 5 * 60 * 1000;
	// Configuration and wikitext are edited by the people reading this page and are not
	// affected by anything the API decides, so they keep the longer window.
	var CONFIG_CACHE_MAX_AGE_MS = 24 * 60 * 60 * 1000;
	var CONTENT_CACHE_MAX_AGE_MS = 24 * 60 * 60 * 1000;
	var PENDING_RETRY_DELAYS_MS = [ 3000, 10000 ];
	// How long the gadget remembers that the API does not serve this wiki. A 404 from
	// the attribution endpoint is a derived, deterministic answer rather than an
	// outage — sites.py decides it without a network call — so it is worth keeping.
	// A week of logs had 17% of all views coming from Commons, Wikidata, Wikisource and
	// Wikinews, where a reader whose script manager loads this file on every project
	// paid one refused request per page for an answer that could never change. A day
	// rather than for ever, because "not served" is also what a misconfigured
	// deployment says and no reader should be locked out of a fixed wiki until they
	// clear their storage.
	var UNSERVED_WIKI_MAX_AGE_MS = 24 * 60 * 60 * 1000;
	// The API answers with the metric that produced the names. Only this one measures who
	// wrote the text on screen, so only this one earns "written by"; any other metric,
	// including one added after this file ships, gets the weaker wording it can support.
	var SURVIVING_TEXT_METRIC = 'wikiwho-surviving-alphanumeric-tokens';

	// The box is drawn before the answer exists, not after. Nothing reaches this gadget
	// in under roughly 280 ms — it opens a fresh connection to another host, so every
	// reader pays DNS, TCP and TLS however warm the data is — and inserting the box on
	// arrival would push the article down under a reader who has already started reading.
	//
	// What it says while it waits is a loading label, not a sentence about the article.
	// It used to be the count sentence with rolling digits standing in for the number,
	// which was a mistake twice over: a reader with reduced motion saw four frozen random
	// digits that read as a fact, and the shape it imitated — "written by N people" — is
	// not the shape of an answer that has names in it, so it was never the final sentence
	// in miniature. A label the reader can dismiss as "not the answer yet" costs nothing
	// and cannot be misread.
	//
	// At PENDING_SETTLE_MS the label gives up and states something true whatever the
	// answer turns out to be, because the retry chain can run for thirteen seconds and a
	// spinner that long is its own kind of lie.
	var PENDING_SETTLE_MS = 2500;

	/**
	 * Every option this gadget reads, and what applies when the configuration page says
	 * nothing. This object is the contract: a key that is not here is not read, so a
	 * page carrying a typo, or one copied from a later revision of the gadget, changes
	 * nothing rather than changing something unpredictable.
	 */
	var DEFAULT_CONFIG = {
		enabled: true,
		showHistoryIntro: 'anonymous',
		editHelpPage: null,
		sandboxPage: null,
		historyIntroPage: null,
		messages: {}
	};

	/**
	 * The values that mean something, for the options whose value is a choice rather
	 * than a page title or free text.
	 *
	 * A value outside its list is not obeyed and not half-obeyed: the default applies.
	 * "showHistoryIntro": "anonymou" must not become a fourth state nobody wrote, and
	 * "enabled": "false" — a string, which is what a hand-edited page tends to grow —
	 * must not switch the gadget off by being merely truthy, nor on by being merely
	 * present.
	 */
	var ALLOWED_VALUES = {
		enabled: [ true, false ],
		showHistoryIntro: [ 'anonymous', 'always', 'never' ]
	};

	/**
	 * Built-in wording. A wiki overrides any key through the "messages" object of its
	 * local configuration page, which is also how a new language gets translated
	 * without touching this script.
	 */
	var MESSAGES = {
		en: {
			'wikipeople-summary-prefix': 'Article written by ',
			// A different measurement deserves a different claim: these accounts edited
			// the page most often, which is not the same as having written what is on it.
			'wikipeople-summary-prefix-edits': 'Article most edited by ',
			'wikipeople-people': '{{PLURAL:$1|$1 person|$1 people}}',
			'wikipeople-others': '{{PLURAL:$1|$1 other person|$1 other people}}',
			'wikipeople-at-least': 'at least $1',
			// Says what the gadget is doing, not what the article is. Nothing here may
			// resemble an answer: it is on screen before one exists.
			'wikipeople-pending': 'Analysing contributions…',
			'wikipeople-many-people': 'many people',
			'wikipeople-user-title': 'View the user page of $1',
			// Stands in for a name that is no longer one. A global rename leaves
			// "Renamed user 4501e2a3c" behind, and crediting an article to a number is
			// worse than saying plainly that the account was anonymised. It is a
			// statement about the account, not about the person: a rename is not
			// necessarily a departure, and this must not claim one.
			'wikipeople-anonymised-account': 'an anonymised account',
			'wikipeople-share': '$1 of the currently visible tokens',
			'wikipeople-share-edits': '$1 of the edits to this page',
			'wikipeople-history-title': 'View the full page history',
			'wikipeople-tooltip': 'Main authors of the text according to WikiWho.',
			'wikipeople-tooltip-edits': 'Accounts that edited this page most, from its history. The text itself could not be analysed.',
			'wikipeople-computed': 'Data computed on $1.',
			'wikipeople-history-intro': 'Each line is one version of the article, showing who changed it.',
			'wikipeople-history-help': 'To get started, read $1 or practise in $2.',
			'wikipeople-history-help-label': 'the editing help',
			'wikipeople-history-sandbox-label': 'the sandbox',
			'wikipeople-history-edit': 'You can also $1.',
			'wikipeople-history-edit-label': 'edit this article directly'
		},
		fr: {
			'wikipeople-summary-prefix': 'Article rédigé par ',
			'wikipeople-summary-prefix-edits': 'Article le plus modifié par ',
			'wikipeople-people': '{{PLURAL:$1|$1 personne|$1 personnes}}',
			'wikipeople-others': '{{PLURAL:$1|$1 autre personne|$1 autres personnes}}',
			'wikipeople-at-least': 'au moins $1',
			'wikipeople-pending': 'Analyse des contributions…',
			'wikipeople-many-people': 'de nombreuses personnes',
			'wikipeople-user-title': 'Voir la page utilisateur de $1',
			'wikipeople-anonymised-account': 'un compte anonymisé',
			'wikipeople-share': '$1 des tokens actuellement visibles',
			'wikipeople-share-edits': '$1 des modifications de la page',
			'wikipeople-history-title': 'Voir l’historique complet de l’article',
			'wikipeople-tooltip': 'Principaux contributeurs du texte selon WikiWho.',
			'wikipeople-tooltip-edits': 'Comptes ayant le plus modifié cette page, d’après son historique. Le texte lui-même n’a pas pu être analysé.',
			'wikipeople-computed': 'Données calculées le $1.',
			'wikipeople-history-intro': 'Chaque ligne correspond à une version de l’article et indique qui l’a modifiée.',
			'wikipeople-history-help': 'Pour commencer, consultez $1 ou entraînez-vous dans $2.',
			'wikipeople-history-help-label': 'l’aide à la modification',
			'wikipeople-history-sandbox-label': 'le bac à sable',
			'wikipeople-history-edit': 'Vous pouvez aussi $1.',
			'wikipeople-history-edit-label': 'modifier directement cet article'
		}
	};

	var config = mw.config.get( [
		'wgAction',
		'wgArticleId',
		'wgCurRevisionId',
		'wgDBname',
		'wgDiffNewId',
		'wgDiffOldId',
		'wgNamespaceNumber',
		'wgPageName',
		'wgRevisionId',
		'wgUserLanguage',
		'wgUserName'
	] );

	var numberFormatter;
	var percentageFormatter;
	var dateFormatter;
	var listFormatter;

	if ( config.wgNamespaceNumber !== 0 || !config.wgArticleId || !config.wgDBname ) {
		return;
	}

	if ( readCache( unservedWikiKey(), UNSERVED_WIKI_MAX_AGE_MS, sharedStorage() ) ) {
		return;
	}

	mw.loader.using( [ 'mediawiki.util', 'mediawiki.Title', 'mediawiki.jqueryMsg' ] )
		.then( loadWikiConfig )
		.then( function ( wikiConfig ) {
			if ( !wikiConfig.enabled ) {
				return;
			}

			installMessages( wikiConfig );
			installFormatters();

			if ( config.wgAction === 'history' ) {
				if ( showsHistoryIntro( wikiConfig ) ) {
					return addHistoryIntroduction( wikiConfig );
				}
				return;
			}

			if (
				config.wgAction === 'view' &&
				!config.wgDiffOldId &&
				!config.wgDiffNewId &&
				config.wgRevisionId === config.wgCurRevisionId
			) {
				return addArticleSummary();
			}
		} )
		.catch( function ( error ) {
			mw.log.warn( 'WikiPeople: initialisation failed', error );
		} );

	/* -------------------------------------------------------------- configuration */

	/**
	 * Title of the configuration page for this reader on this wiki, or null when there
	 * is nobody to attribute it to. Namespace 2 resolves to the wiki's own localised
	 * user-namespace name, so this works unchanged on every language edition.
	 */
	function configPage() {
		if ( !config.wgUserName ) {
			return null;
		}
		return new mw.Title( config.wgUserName + CONFIG_PAGE_SUFFIX, 2 ).getPrefixedDb();
	}

	/**
	 * Read the configuration page. A missing page is the normal case for someone who has
	 * not customised anything, so a 404 resolves to the defaults rather than rejecting.
	 */
	async function loadWikiConfig() {
		var page = configPage();
		var cacheKey;
		var cached;
		var url;
		var response;
		var parsed;

		if ( !page ) {
			return normalizeConfig( {} );
		}

		// The page is per user as well as per wiki, so both belong in the key: a shared
		// browser must not serve one account's configuration to the next.
		cacheKey = 'wikipeople:config:' + CACHE_VERSION + ':' + config.wgDBname + ':' + page;
		cached = readCache( cacheKey, CONFIG_CACHE_MAX_AGE_MS );

		if ( cached ) {
			return normalizeConfig( cached );
		}

		url = mw.util.wikiScript( 'index' ) +
			'?title=' + encodeURIComponent( page ) +
			'&action=raw&ctype=application/json';

		try {
			response = await fetch( url, {
				headers: { Accept: 'application/json' },
				credentials: 'omit'
			} );
			parsed = response.ok ? await response.json() : {};
		} catch ( error ) {
			parsed = {};
		}

		// Cached as the page wrote it and normalised on the way out. Storing the
		// normalised object instead would bake today's defaults into the cache for a
		// day, so a reader who updates the gadget would keep the old behaviour until
		// the entry expired.
		writeCache( cacheKey, parsed );
		return normalizeConfig( parsed );
	}

	/**
	 * The configuration this reader actually gets: the defaults, with each option the
	 * page sets to a value the gadget accepts laid over the top.
	 *
	 * Nothing here trusts the page. It is JSON written by hand, on a wiki, possibly
	 * years ago against a different revision of this file, so every value is checked
	 * against what its option can be and a rejected one falls back to its default
	 * rather than reaching the code that consumes it.
	 */
	function normalizeConfig( parsed ) {
		var wikiConfig = Object.assign( {}, DEFAULT_CONFIG );

		if ( !parsed || typeof parsed !== 'object' || Array.isArray( parsed ) ) {
			return wikiConfig;
		}

		Object.keys( DEFAULT_CONFIG ).forEach( function ( key ) {
			var value = parsed[ key ];

			// showHistoryIntro was a boolean before it grew a third state, and pages
			// written against that revision are still on wikis. They keep meaning what
			// they said rather than quietly reverting to the default.
			if ( key === 'showHistoryIntro' && typeof value === 'boolean' ) {
				value = value ? 'always' : 'never';
			}

			if ( ALLOWED_VALUES[ key ] ) {
				if ( ALLOWED_VALUES[ key ].indexOf( value ) !== -1 ) {
					wikiConfig[ key ] = value;
				}
				return;
			}

			if ( key === 'messages' ) {
				if ( value && typeof value === 'object' && !Array.isArray( value ) ) {
					wikiConfig[ key ] = value;
				}
				return;
			}

			// Everything left is a page title. An empty string is how a page says "none"
			// while keeping the key visible, and a link to a page called "" is worse
			// than no link at all.
			if ( typeof value === 'string' && value.trim() ) {
				wikiConfig[ key ] = value.trim();
			}
		} );

		return wikiConfig;
	}

	/**
	 * Whether the history box renders for this reader.
	 *
	 * The box explains what a page history is. That is worth the space for someone who
	 * has never seen one and is noise for someone who came to the page to read it, so
	 * the default shows it to logged-out readers only. Being logged in is not the same
	 * as knowing the wiki, but it is the only signal available here — and the reader it
	 * gets wrong is precisely the one who can open a configuration page and say
	 * "always".
	 *
	 * While this is a personal script the configuration page is the reader's own, so a
	 * logged-out reader never has one and always gets this default. The option earns
	 * its third state when the page moves to MediaWiki: space and one setting covers
	 * everybody.
	 */
	function showsHistoryIntro( wikiConfig ) {
		if ( wikiConfig.showHistoryIntro === 'always' ) {
			return true;
		}
		if ( wikiConfig.showHistoryIntro === 'never' ) {
			return false;
		}
		return !config.wgUserName;
	}

	/* ------------------------------------------------------------- custom content */

	/**
	 * Titles to try for a reader's own rich content, most specific first.
	 *
	 * A wikitext page is written in one language, so translations live on language
	 * subpages: /fr-ca, then /fr, then the base title. That keeps one reviewable page
	 * per language instead of one page in whichever language its author happened to
	 * speak, which is the problem the flat "messages" object has.
	 */
	function contentCandidates( base ) {
		var language = config.wgUserLanguage || 'en';
		var candidates = [ base + '/' + language ];

		if ( language.indexOf( '-' ) !== -1 ) {
			candidates.push( base + '/' + language.split( '-' )[ 0 ] );
		}
		candidates.push( base );
		return candidates;
	}

	/**
	 * Parsed HTML for one title, or null when it does not exist.
	 *
	 * Anonymous so the response stays CDN-cacheable, and asks for a short server-side
	 * cache: an introduction changes rarely but is read on every history view.
	 */
	async function fetchParsedPage( title ) {
		var url = mw.util.wikiScript( 'api' ) +
			'?action=parse&format=json&formatversion=2&prop=text' +
			'&redirects=1&disablelimitreport=1&disableeditsection=1' +
			'&smaxage=300&maxage=300' +
			'&page=' + encodeURIComponent( title );
		var response;
		var data;

		try {
			response = await fetch( url, {
				headers: { Accept: 'application/json' },
				credentials: 'omit'
			} );
			if ( !response.ok ) {
				return null;
			}
			data = await response.json();
		} catch ( error ) {
			return null;
		}

		// A missing page reports an error code rather than an HTTP status, and is the
		// normal case for a reader who has not written one.
		if ( !data || !data.parse || typeof data.parse.text !== 'string' ) {
			return null;
		}
		return data.parse.text;
	}

	/**
	 * Rich introduction for this reader, already parsed by MediaWiki, or null.
	 *
	 * Wikitext buys images, galleries, Commons video and templates for nothing, and the
	 * parser sanitises it, so this script never has to build markup from a string or
	 * trust the page to be well-behaved.
	 */
	async function loadCustomContent( title ) {
		var cacheKey;
		var cached;
		var candidates;
		var index;
		var html = null;

		if ( typeof title !== 'string' || !title ) {
			return null;
		}

		cacheKey = 'wikipeople:content:' + CACHE_VERSION + ':' + config.wgDBname + ':' +
			title + ':' + ( config.wgUserLanguage || 'en' );
		cached = readCache( cacheKey, CONTENT_CACHE_MAX_AGE_MS );

		if ( cached ) {
			return cached.html;
		}

		candidates = contentCandidates( title );

		for ( index = 0; index < candidates.length && !html; index++ ) {
			html = await fetchParsedPage( candidates[ index ] );
		}

		// The absence of a page is cached too. Otherwise a configured but unwritten
		// title costs three failed lookups on every single history view.
		writeCache( cacheKey, { html: html } );
		return html;
	}

	/**
	 * Turn parser output into nodes.
	 *
	 * DOMParser builds an inert document, so nothing loads or runs while the fragment is
	 * assembled. Wikitext cannot produce a script element in the first place; removing
	 * any that appear keeps that true of this function on its own, without having to
	 * reason about the whole parser pipeline to review it.
	 */
	function renderCustomContent( html ) {
		var parsed = new DOMParser().parseFromString( html, 'text/html' );
		var container = document.createElement( 'div' );

		parsed.querySelectorAll( 'script' ).forEach( function ( node ) {
			node.remove();
		} );

		// A note box is not worth blocking a page render for: media loads when reached,
		// and video never starts on its own in something the reader did not ask to play.
		parsed.querySelectorAll( 'img' ).forEach( function ( image ) {
			image.setAttribute( 'loading', 'lazy' );
		} );
		parsed.querySelectorAll( 'video' ).forEach( function ( video ) {
			video.removeAttribute( 'autoplay' );
			video.setAttribute( 'preload', 'none' );
		} );

		if ( parsed.querySelector( 'video, .mw-tmh-player' ) ) {
			// Commons video falls back to a bare player without TimedMediaHandler, and
			// the module is absent on some wikis, so failing to load it is not an error.
			mw.loader.using( 'ext.tmh.player' ).catch( function () {} );
		}

		container.className = 'wikipeople-custom';
		container.append.apply(
			container,
			Array.prototype.slice.call( parsed.body.childNodes )
		);
		return container;
	}

	function installMessages( wikiConfig ) {
		var language = config.wgUserLanguage || 'en';
		var base = language.split( '-' )[ 0 ];
		var table = Object.assign(
			{},
			MESSAGES.en,
			MESSAGES[ base ] || {},
			MESSAGES[ language ] || {}
		);

		if ( wikiConfig.messages && typeof wikiConfig.messages === 'object' ) {
			Object.keys( wikiConfig.messages ).forEach( function ( key ) {
				if ( typeof wikiConfig.messages[ key ] === 'string' ) {
					table[ key ] = wikiConfig.messages[ key ];
				}
			} );
		}

		mw.messages.set( table );
	}

	/**
	 * Not every MediaWiki language code is a valid BCP 47 tag, so each formatter falls
	 * back through the base language to English rather than throwing.
	 */
	function safeFormatter( build ) {
		var language = config.wgUserLanguage || 'en';
		var candidates = [ language, language.split( '-' )[ 0 ], 'en' ];
		var index;

		for ( index = 0; index < candidates.length; index++ ) {
			try {
				return build( candidates[ index ] );
			} catch ( error ) {
				continue;
			}
		}
		return null;
	}

	function installFormatters() {
		numberFormatter = safeFormatter( function ( locale ) {
			return new Intl.NumberFormat( locale );
		} );
		percentageFormatter = safeFormatter( function ( locale ) {
			return new Intl.NumberFormat( locale, {
				maximumFractionDigits: 1,
				style: 'percent'
			} );
		} );
		dateFormatter = safeFormatter( function ( locale ) {
			return new Intl.DateTimeFormat( locale, {
				day: 'numeric',
				month: 'long',
				year: 'numeric'
			} );
		} );
		listFormatter = typeof Intl !== 'undefined' && Intl.ListFormat ?
			safeFormatter( function ( locale ) {
				return new Intl.ListFormat( locale, { style: 'long', type: 'conjunction' } );
			} ) :
			null;
	}

	function formatNumber( value ) {
		return numberFormatter ? numberFormatter.format( value ) : String( value );
	}

	/* ------------------------------------------------------------------ rendering */

	function insertBelowSubtitle( element ) {
		var siteSub = document.getElementById( 'siteSub' );
		var vectorSlot = document.querySelector( '.vector-body-before-content' );
		var bodyContent = document.getElementById( 'bodyContent' );

		if ( siteSub ) {
			siteSub.after( element );
			return true;
		}

		if ( vectorSlot ) {
			vectorSlot.prepend( element );
			return true;
		}

		if ( bodyContent ) {
			bodyContent.prepend( element );
			return true;
		}

		return false;
	}

	/**
	 * Replace $1, $2… in a message with DOM nodes, so a sentence can contain links
	 * without ever building HTML from a string.
	 */
	function appendMessageWithNodes( parent, messageKey, nodes ) {
		var text = mw.message( messageKey ).text();
		var pattern = /\$(\d+)/g;
		var lastIndex = 0;
		var match;

		while ( ( match = pattern.exec( text ) ) !== null ) {
			if ( match.index > lastIndex ) {
				parent.append( document.createTextNode( text.slice( lastIndex, match.index ) ) );
			}
			parent.append( nodes[ Number( match[ 1 ] ) - 1 ] || document.createTextNode( '' ) );
			lastIndex = match.index + match[ 0 ].length;
		}

		if ( lastIndex < text.length ) {
			parent.append( document.createTextNode( text.slice( lastIndex ) ) );
		}
	}

	async function addHistoryIntroduction( wikiConfig ) {
		var box;
		var line;
		var custom;

		if ( document.getElementById( HISTORY_INTRO_ID ) ) {
			return;
		}

		box = document.createElement( 'div' );
		box.id = HISTORY_INTRO_ID;
		box.className = 'wikipeople wikipeople--history';
		box.setAttribute( 'role', 'note' );

		custom = await loadCustomContent( wikiConfig.historyIntroPage );

		if ( custom ) {
			box.append( renderCustomContent( custom ) );
		} else {
			line = document.createElement( 'p' );
			line.textContent = mw.message( 'wikipeople-history-intro' ).text();
			box.append( line );

			// The help and sandbox pages have no cross-wiki names, so this sentence only
			// appears where the local configuration page supplies both titles.
			if ( wikiConfig.editHelpPage && wikiConfig.sandboxPage ) {
				line = document.createElement( 'p' );
				appendMessageWithNodes( line, 'wikipeople-history-help', [
					createWikiLink(
						wikiConfig.editHelpPage,
						mw.message( 'wikipeople-history-help-label' ).text()
					),
					createWikiLink(
						wikiConfig.sandboxPage,
						mw.message( 'wikipeople-history-sandbox-label' ).text()
					)
				] );
				box.append( line );
			}
		}

		// Always built here, never in wikitext: a page parsed on its own has no idea
		// which article the reader is looking at, so {{FULLPAGENAME}} would name the
		// introduction itself and the link would offer to edit the wrong page.
		line = document.createElement( 'p' );
		appendMessageWithNodes( line, 'wikipeople-history-edit', [ createEditLink() ] );
		box.append( line );

		insertBelowSubtitle( box );
		mw.hook( 'wikipeople.history' ).fire( box, wikiConfig );

		// After the box is in the page, never before. The introduction is the reason the
		// reader is looking, and it must not wait on an API round trip to appear.
		return fillContributorCount( box );
	}

	/**
	 * Put this article's contributor count into any slot the custom content declared.
	 *
	 * A wikitext page is parsed on its own and cached for every article, so it cannot
	 * contain a per-article number. It writes an element of a known class instead, keeps
	 * plain wording inside it as a fallback, and this replaces that text once the API
	 * answers. A page that asks for no count triggers no request at all, which is what
	 * keeps history views free for everyone who does not use this.
	 */
	async function fillContributorCount( box ) {
		var phrases = box.querySelectorAll( '.wikipeople-count' );
		var numbers = box.querySelectorAll( '.wikipeople-number' );
		var data;
		var phrase;

		if ( !phrases.length && !numbers.length ) {
			return;
		}

		try {
			// No retry here, unlike an article view: a result that is still being computed
			// should leave the reader's own wording alone rather than rewrite the box under
			// them ten seconds after they started reading it.
			data = await contributionData( [] );
		} catch ( error ) {
			mw.log.warn( 'WikiPeople: contributor count unavailable', error );
			return;
		}

		if ( !data || data.humanCount < 1 ) {
			return;
		}

		phrase = mw.message( 'wikipeople-people', formatNumber( data.humanCount ) ).text();
		if ( data.limited ) {
			phrase = mw.message( 'wikipeople-at-least', phrase ).text();
		}

		phrases.forEach( function ( slot ) {
			slot.textContent = phrase;
		} );
		numbers.forEach( function ( slot ) {
			slot.textContent = formatNumber( data.humanCount );
		} );
	}

	/**
	 * What the box should say right now.
	 *
	 * 'loading' the gadget is working; the box says so and claims nothing else.
	 * 'vague'   wording that holds whatever the answer turns out to be.
	 * 'final'   the real sentence.
	 *
	 * There is no state for an empty page: the box exists from the first frame, so that
	 * the sentence lands in space already reserved for it rather than shoving the
	 * article aside three hundred milliseconds in.
	 *
	 * Separated from the DOM so the whole timing policy is five readable lines. Reduced
	 * motion is not a state here any more: there is nothing left that moves.
	 */
	function countDisplayState( elapsedMs, data ) {
		if ( data ) {
			return 'final';
		}
		if ( elapsedMs < PENDING_SETTLE_MS ) {
			return 'loading';
		}
		return 'vague';
	}

	/**
	 * A box that says the gadget is working, and nothing about the article.
	 *
	 * `aria-busy` marks it as in progress; the label itself is stable and meaningful, so
	 * unlike the digits it replaced there is no reason to hide it from a screen reader.
	 */
	function buildPendingSummary() {
		var box = document.createElement( 'div' );

		box.id = ARTICLE_SUMMARY_ID;
		box.className = 'wikipeople wikipeople--article wikipeople-pending';
		box.setAttribute( 'role', 'note' );
		box.setAttribute( 'aria-busy', 'true' );
		box.textContent = mw.message( 'wikipeople-pending' ).text();
		return box;
	}

	/**
	 * Stop promising an answer and say something that stays true if none comes.
	 *
	 * This is a refinement, not a correction: vague then precise, never wrong then right.
	 * That is what separates it from showing a number the API would later contradict.
	 */
	function settlePendingSummary( box ) {
		box.textContent = mw.message( 'wikipeople-summary-prefix' ).text() +
			mw.message( 'wikipeople-many-people' ).text() + '.';
		box.classList.remove( 'wikipeople-pending' );
		box.removeAttribute( 'aria-busy' );
	}

	/**
	 * Hold the box until the request resolves, one way or another.
	 *
	 * The box goes in on the first pass, before the first await, so it is part of the
	 * page from the moment the gadget runs. Exits as soon as `outcome.done` is set.
	 */
	async function runCountPlaceholder( startedAt, outcome ) {
		var box = buildPendingSummary();

		// An unrecognised skin offers nowhere to put it, and there is no point holding
		// space in a page that will never receive the sentence.
		if ( !insertBelowSubtitle( box ) ) {
			return;
		}

		while ( !outcome.done ) {
			if ( countDisplayState( Date.now() - startedAt, null ) === 'loading' ) {
				// Nothing redraws while it waits, so this is one timer rather than
				// thirty. 'loading' only occurs below the threshold, so the delay stays
				// positive and the loop cannot spin.
				await wait( PENDING_SETTLE_MS - ( Date.now() - startedAt ) );
				continue;
			}

			// Settled. Let the awaited request replace the wording if it ever arrives.
			settlePendingSummary( box );
			return;
		}
	}

	function removeArticleSummary() {
		var existing = document.getElementById( ARTICLE_SUMMARY_ID );

		if ( existing ) {
			existing.remove();
		}
	}

	/**
	 * Render the attribution sentence, filling it in as the answer arrives.
	 *
	 * The placeholder appears only when the wait is real: a cached answer skips it and
	 * renders straight away. A page the API cannot serve — an unsupported wiki, a network
	 * failure — must leave no trace, which is why an error is told apart from a result
	 * that is merely still being computed.
	 */
	async function addArticleSummary() {
		var startedAt = Date.now();
		var outcome = { done: false, data: null, failed: false };
		var existing;
		var pending;
		var summary;

		if ( document.getElementById( ARTICLE_SUMMARY_ID ) ) {
			return;
		}

		// Read the session cache before drawing anything. A cached answer needs no
		// network, so there is no wait for a placeholder to explain — and drawing one
		// anyway would flash "analysing" on every revisit, which is the common path for
		// anyone moving between an article and its history.
		outcome.data = readCache( getCacheKey(), CLIENT_CACHE_MAX_AGE_MS );

		if ( !outcome.data ) {
			pending = contributionData( PENDING_RETRY_DELAYS_MS ).then( function ( value ) {
				outcome.data = value;
			}, function ( error ) {
				mw.log.warn( 'WikiPeople: attribution unavailable', error );
				outcome.failed = true;
			} ).then( function () {
				outcome.done = true;
			} );

			await Promise.all( [ pending, runCountPlaceholder( startedAt, outcome ) ] );
		}

		// An error is not a slow answer: an unsupported wiki must not be left claiming
		// that many people wrote the article.
		if ( outcome.failed ) {
			removeArticleSummary();
			return;
		}

		// Still pending after every retry. The settled wording is the final answer.
		if ( !outcome.data ) {
			return;
		}

		summary = buildArticleSummary( outcome.data );
		if ( !summary ) {
			removeArticleSummary();
			return;
		}

		existing = document.getElementById( ARTICLE_SUMMARY_ID );
		if ( existing ) {
			existing.replaceWith( summary );
		} else {
			insertBelowSubtitle( summary );
		}
		mw.hook( 'wikipeople.summary' ).fire( summary, outcome.data );
	}

	/**
	 * Attribution for this page, from the session cache when it is there.
	 *
	 * The cache key is the page, not the view, so a reader who looks at an article and
	 * then opens its history pays for one request rather than two. How long to wait on a
	 * result that is still being computed is the caller's decision.
	 */
	async function contributionData( retryDelaysMs ) {
		var cacheKey = getCacheKey();
		var cached = readCache( cacheKey, CLIENT_CACHE_MAX_AGE_MS );
		var data;

		if ( cached ) {
			return cached;
		}

		data = await loadContributionData( retryDelaysMs );
		if ( data ) {
			writeCache( cacheKey, data );
		}
		return data;
	}

	async function loadContributionData( retryDelaysMs ) {
		var url = TOOLFORGE_API_BASE + '/v2/' +
			encodeURIComponent( config.wgDBname ) + '/pages/' +
			encodeURIComponent( config.wgArticleId ) +
			'?revision_id=' + encodeURIComponent( config.wgCurRevisionId );
		var delays = retryDelaysMs || PENDING_RETRY_DELAYS_MS;
		var attempt;
		var data;

		for ( attempt = 0; attempt <= delays.length; attempt++ ) {
			try {
				data = await fetchJson( url, {
					credentials: 'omit',
					referrerPolicy: 'no-referrer'
				} );
			} catch ( error ) {
				if ( error && error.status === 404 ) {
					writeCache( unservedWikiKey(), true, sharedStorage() );
				}
				throw error;
			}

			if ( data.status === 'ready' ) {
				if (
					typeof data.distinct_contributors !== 'number' ||
					!Array.isArray( data.contributors )
				) {
					throw new Error( 'Invalid attribution response.' );
				}

				return normalizeContributionData( data );
			}

			if ( data.status !== 'pending' ) {
				throw new Error( 'Unknown attribution state.' );
			}

			if ( attempt < delays.length ) {
				await wait( delays[ attempt ] );
				await whenVisible();
			}
		}

		return null;
	}

	/**
	 * Resolve once the tab is on screen, immediately when it already is.
	 *
	 * Browsers throttle timers in a background tab, which is exactly the tab this
	 * matters for: opening a dozen articles with the middle mouse button fires a dozen
	 * first requests and then no retry until the reader arrives, by which time the
	 * three-second and thirteen-second marks are long past. Over a week, 104 of the 153
	 * views that showed nothing had made a single request, while the result they were
	 * waiting for was stored a median two seconds later. Spending the two retries when
	 * the tab is looked at rather than when a clock says so costs nothing on a
	 * foreground page and is the whole difference on a backgrounded one.
	 */
	function whenVisible() {
		if ( document.visibilityState !== 'hidden' ) {
			return Promise.resolve();
		}
		return new Promise( function ( resolve ) {
			document.addEventListener( 'visibilitychange', function onVisibilityChange() {
				if ( document.visibilityState !== 'hidden' ) {
					document.removeEventListener( 'visibilitychange', onVisibilityChange );
					resolve();
				}
			} );
		} );
	}

	function normalizeContributionData( data ) {
		return {
			computedAt: data.computed_at,
			humanCount: data.distinct_contributors,
			limited: Boolean( data.count_limited ),
			// Which rung of the ladder answered. The names alone do not say whether they
			// wrote the text or merely edited it most, and the sentence has to.
			wroteTheText: data.metric === SURVIVING_TEXT_METRIC,
			topEditors: data.contributors.slice( 0, 3 ).map( function ( editor ) {
				return {
					share: Number( editor.share ),
					username: editor.username,
					// How the API says this name may be shown. Absent means "as a link",
					// which is what every answer said before the field existed.
					display: editor.display || 'link'
				};
			} )
		};
	}

	function wait( delayMs ) {
		return new Promise( function ( resolve ) {
			window.setTimeout( resolve, delayMs );
		} );
	}

	async function fetchJson( url, options ) {
		var controller = new AbortController();
		var timeout = window.setTimeout( function () {
			controller.abort();
		}, REQUEST_TIMEOUT_MS );
		var response;
		var error;

		try {
			response = await fetch( url, Object.assign( {
				headers: {
					Accept: 'application/json'
				},
				signal: controller.signal
			}, options ) );

			if ( !response.ok ) {
				error = new Error( 'HTTP error ' + response.status );
				// The caller cannot ask the Response once this has been thrown, and one
				// status means something no other does: 404 is the API saying it will
				// never serve this wiki, not that this request went wrong.
				error.status = response.status;
				throw error;
			}

			return await response.json();
		} finally {
			window.clearTimeout( timeout );
		}
	}

	function buildArticleSummary( data ) {
		var box = document.createElement( 'div' );
		var topEditors = data.topEditors;
		var otherCount = Math.max( 0, data.humanCount - topEditors.length );
		var computedDate = new Date( data.computedAt );
		// Only a named ranking can be weaker than "written by". With no names the
		// sentence is about the count alone, which the edit history and the token
		// analysis agree on, so it keeps the ordinary wording.
		var namedByEditCount = topEditors.length > 0 && !data.wroteTheText;
		var tooltip = [];
		var nodes;

		if ( data.humanCount < 1 ) {
			return null;
		}

		box.id = ARTICLE_SUMMARY_ID;
		box.className = 'wikipeople wikipeople--article';
		box.setAttribute( 'role', 'note' );
		// Both tooltips describe a ranking, so neither belongs on a box that shows none.
		// A page WikiWho refused can still reach this branch, and saying its names come
		// from WikiWho would then credit a service that never saw the article.
		if ( topEditors.length ) {
			tooltip.push( mw.message(
				namedByEditCount ? 'wikipeople-tooltip-edits' : 'wikipeople-tooltip'
			).text() );
		}
		if ( !Number.isNaN( computedDate.getTime() ) && dateFormatter ) {
			tooltip.push( mw.message(
				'wikipeople-computed',
				dateFormatter.format( computedDate )
			).text() );
		}
		box.title = tooltip.join( ' ' );

		box.append( document.createTextNode( mw.message(
			namedByEditCount ? 'wikipeople-summary-prefix-edits' : 'wikipeople-summary-prefix'
		).text() ) );

		if ( topEditors.length ) {
			nodes = topEditors.map( function ( editor ) {
				return createEditorLink( editor, namedByEditCount );
			} );
			if ( otherCount > 0 ) {
				nodes.push( createHistoryCountLink( otherCount, data.limited, true ) );
			}
		} else {
			nodes = [ createHistoryCountLink( data.humanCount, data.limited, false ) ];
		}

		appendList( box, nodes );
		box.append( document.createTextNode( '.' ) );
		return box;
	}

	/**
	 * Join the links with the conjunction of the reader's language. Intl.ListFormat
	 * knows that English wants "A, B and C" while other languages differ, so the
	 * separator is never hard-coded. Placeholders carry the node index through
	 * formatToParts, which keeps real DOM elements in the sentence.
	 */
	function appendList( parent, nodes ) {
		var placeholders;
		var parts;

		if ( nodes.length === 1 ) {
			parent.append( nodes[ 0 ] );
			return;
		}

		if ( !listFormatter ) {
			nodes.forEach( function ( node, index ) {
				if ( index > 0 ) {
					parent.append( document.createTextNode( ', ' ) );
				}
				parent.append( node );
			} );
			return;
		}

		placeholders = nodes.map( function ( _node, index ) {
			return '\u0000' + index;
		} );
		parts = listFormatter.formatToParts( placeholders );

		parts.forEach( function ( part ) {
			if ( part.type === 'element' ) {
				parent.append( nodes[ Number( part.value.slice( 1 ) ) ] );
			} else {
				parent.append( document.createTextNode( part.value ) );
			}
		} );
	}

	/**
	 * One contributor, as a link or as plain text, according to what the API allowed.
	 *
	 * "link" is the ordinary case. "unlink" is the same name with no link, and the
	 * reason is deliberately not sent: it covers an account with no user page, and it
	 * covers whatever else a wiki has decided to unlink, so nothing here may explain it
	 * — an explanation would turn one opaque value back into a disclosure. "label"
	 * replaces the name, for an account whose name is a placeholder left by a rename.
	 *
	 * Only the share is said about an unlinked name, because the share is true whatever
	 * the reason was.
	 */
	function createEditorLink( editor, byEditCount ) {
		var isLabel = editor.display === 'label';
		var isLinked = editor.display === 'link';
		var node = document.createElement( isLinked ? 'a' : 'span' );
		var name = editor.username.replace( /_/g, ' ' );
		var tooltip = [];

		if ( isLabel ) {
			node.textContent = mw.message( 'wikipeople-anonymised-account' ).text();
			node.className = 'wikipeople-anonymised';
		} else {
			node.textContent = name;
			if ( !isLinked ) {
				// A name is a name whether or not it links, so it keeps the weight the
				// linked ones have. Without this it would read as the odd one out in a
				// sentence, which is a statement about the person the API declined to
				// make.
				node.className = 'wikipeople-unlinked';
			}
		}

		if ( isLinked ) {
			node.href = new mw.Title( editor.username, 2 ).getUrl();
			tooltip.push( mw.message( 'wikipeople-user-title', name ).text() );
		}

		if ( Number.isFinite( editor.share ) && percentageFormatter ) {
			// The share is a share of whatever was ranked: of the visible tokens, or of
			// the page's edits. Naming the wrong one turns a true percentage into a
			// false statement.
			tooltip.push( mw.message(
				byEditCount ? 'wikipeople-share-edits' : 'wikipeople-share',
				percentageFormatter.format( editor.share )
			).text() );
		}

		if ( tooltip.length ) {
			node.title = tooltip.join( ' — ' );
		}
		return node;
	}

	function createHistoryCountLink( count, limited, isRemainder ) {
		var link = document.createElement( 'a' );
		var label = mw.message(
			isRemainder ? 'wikipeople-others' : 'wikipeople-people',
			formatNumber( count )
		).text();

		if ( limited ) {
			label = mw.message( 'wikipeople-at-least', label ).text();
		}

		link.href = mw.util.getUrl( config.wgPageName, { action: 'history' } );
		link.textContent = label;
		link.title = mw.message( 'wikipeople-history-title' ).text();
		return link;
	}

	function createWikiLink( title, label ) {
		var link = document.createElement( 'a' );
		link.href = mw.util.getUrl( title );
		link.textContent = label;
		return link;
	}

	function createEditLink() {
		var link = document.createElement( 'a' );
		link.href = mw.util.getUrl( config.wgPageName, { veaction: 'edit' } );
		link.textContent = mw.message( 'wikipeople-history-edit-label' ).text();
		return link;
	}

	/* -------------------------------------------------------------------- caching */

	function getCacheKey() {
		return 'wikipeople:' + CACHE_VERSION + ':' +
			config.wgDBname + ':' +
			config.wgArticleId;
	}

	/**
	 * A key that has to outlive the tab, unlike everything else cached here.
	 *
	 * Which wiki the API serves is the same answer in every tab, and the reader who
	 * pays for asking is precisely the one who opens many at once. Session storage
	 * would make each of those ask again.
	 */
	function unservedWikiKey() {
		return 'wikipeople:unserved:' + CACHE_VERSION + ':' + config.wgDBname;
	}

	function sharedStorage() {
		return window.localStorage;
	}

	function readCache( key, maxAgeMs, storage ) {
		var raw;
		var cached;

		try {
			raw = ( storage || window.sessionStorage ).getItem( key );
			cached = raw ? JSON.parse( raw ) : null;
			if (
				!cached ||
				typeof cached.storedAt !== 'number' ||
				Date.now() - cached.storedAt > maxAgeMs
			) {
				( storage || window.sessionStorage ).removeItem( key );
				return null;
			}
			return cached.data || null;
		} catch ( error ) {
			return null;
		}
	}

	function writeCache( key, data, storage ) {
		try {
			( storage || window.sessionStorage ).setItem( key, JSON.stringify( {
				data: data,
				storedAt: Date.now()
			} ) );
		} catch ( error ) {
			// The gadget stays functional when storage is disabled or full.
		}
	}
}() );
