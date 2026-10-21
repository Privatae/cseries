/* ================================================================
 * Getting Started — the slides. One entry per slide, in order.
 *
 * A slide is: a title, a few plain lines, and ONE button. On an app
 * slide the button opens that app; when the app's window closes (or
 * the user comes back to this window) the slide's check runs against
 * the host, and a pass moves to the next slide. That is the whole
 * flow — the engine (app.js) has nothing else in it.
 *
 * Check shape: { endpoint, evaluate(data) -> bool, notYet }
 *   endpoint  — GET, declared as data so scripts/test_app_manifest_scopes.py
 *               can prove the manifest covers it.
 *   evaluate  — true means the slide is done.
 *   notYet    — the one line shown when the check comes back false.
 *
 * appView (optional) opens the slide's app at that view, through the
 * shell's deep-link lane.
 * ================================================================ */

// The name a new Host gives itself (server/local_identity.py DEFAULT_NAME).
const GS_DEFAULT_HOST_NAME = 'C-Series';

window.GS_SLIDES = [
    {
        id: 'welcome',
        title: 'Welcome to your host',
        lines: [
            'A host is your own machine that stays on and runs one AI that knows your setup.',
            'A few short steps get it working. Each one opens the real app; close the app when you\'re done and the tour moves on. Anything you\'d rather not do now, skip — nothing here locks you in.',
        ],
        button: 'Start',
    },
    {
        // The name is saved in Settings, not here: renaming the Host takes
        // Settings' own token, which only Settings' own origin can hold
        // (server/routes/host_name.py says why this app cannot hold it).
        id: 'name',
        appId: 'settings',
        appName: 'Settings',
        appView: 'overview',
        title: 'Name your Host',
        lines: [
            'Your Host answers to a name. It starts as "C-Series"; call it whatever you like.',
            'Open Settings, type the name under About and press Save. Skip this to keep "C-Series"; you can change it in Settings any time.',
        ],
        button: 'Open Settings',
        check: {
            // Done once the Host has a name of its own.
            endpoint: '/api/system/status',
            evaluate: (d) => {
                const n = String((d && d.entity_display_name) || '').trim();
                return !!n && n !== GS_DEFAULT_HOST_NAME && !/^Host #\d+$/.test(n);
            },
            notYet: 'Still called "C-Series". Type a name under About in Settings and save it, or skip to keep it.',
        },
    },
    {
        id: 'keys',
        appId: 'keys',
        appName: 'Keys',
        title: 'Add one provider key',
        lines: [
            'Your host thinks with AI models from a provider — OpenRouter, Anthropic, OpenAI, and others. A provider gives you an API key; that key is what lets your host use their models on your account.',
            'Open Keys, pick a provider, paste the key, and close the window.',
        ],
        button: 'Open Keys',
        check: {
            endpoint: '/api/vault/providers',
            // source !== 'host': this step tells the user to PASTE a key, so a
            // sign-in the HOST filled (by connecting a builder) must not satisfy
            // it — that would advance the tour on something the user never did,
            // and the next step's routing runs on the API key anyway.
            evaluate: (d) => ((d && d.providers) || []).some(
                (p) => ((p && p.slots) || []).some(
                    (s) => s && s.stored && !s.deactivated && s.source !== 'host')),
            notYet: 'No key stored yet. Open Keys again and paste one — or skip for now.',
        },
    },
    {
        id: 'orchestrator',
        appId: 'orchestrator',
        appName: 'Orchestrator',
        title: 'Pick your setup',
        lines: [
            'The Orchestrator decides which model answers each kind of job. It opens on a simple page: choose the provider you just added, click once, and every job is routed.',
            'Open it, apply the recommended setup, and close the window.',
        ],
        button: 'Open Orchestrator',
        check: {
            endpoint: '/api/inference/routing',
            evaluate: (d) => Object.values((d && d.effective) || {}).some(
                (e) => e && e.source && e.source !== 'unconfigured'),
            notYet: 'Nothing is routed yet. Open the Orchestrator again and apply the recommended setup — or skip for now.',
        },
    },
    {
        // Optional. Nothing on a Host needs the network: the App Store works
        // without any account. The check reads membership, so a Host that
        // already joined passes quietly.
        id: 'privatae-network',
        appId: 'settings',
        appName: 'Settings',
        appView: 'network',
        title: 'Join the Privatae network (optional)',
        lines: [
            'Apps install from the App Store without any account.',
            'A membership adds the Privatae network: the community board, and a place to keep your Host\'s encrypted backups in the network\'s storage. Get a membership code at privatae.ai/account/network and enter it in Settings, under Network.',
            'Skip this if you don\'t want it; you can join later.',
        ],
        button: 'Open Settings',
        check: {
            endpoint: '/api/network/status',
            evaluate: (d) => !!(d && d.member),
            notYet: 'Not joined yet. Enter your code in Settings → Network, or skip; everything else works without it.',
        },
    },
    {
        id: 'circles',
        appId: 'circles',
        appName: 'Circles',
        title: 'Ask your host something',
        lines: [
            'Circles is where you talk with your host. Ask anything, the way you\'d ask a person.',
            'Open it, send one message, wait for the answer, and close the window.',
        ],
        button: 'Open Circles',
        check: {
            endpoint: '/api/circles/thread/self/self',
            evaluate: (d) => ((d && d.messages) || []).some(
                (m) => m && m.author_kind === 'entity'),
            notYet: 'No answer yet. Open Circles again and send a message — or skip for now.',
        },
    },
    {
        id: 'workbench',
        appId: 'workbench',
        appName: 'Workbench',
        title: 'Start your first build',
        lines: [
            'The Workbench is where your host builds things — a tool, a page, an automation. Each build is a project: you describe it, your host plans it with you, then does the work.',
            'Open it, start a project, and close the window.',
        ],
        button: 'Open Workbench',
        check: {
            endpoint: '/api/projects?limit=5',
            evaluate: (d) => (Array.isArray(d) ? d : ((d && d.projects) || [])).length > 0,
            notYet: 'No project yet. Open the Workbench again and start one — or skip for now.',
        },
    },
    {
        id: 'done',
        title: 'You\'re set up',
        lines: [
            'Your host has a key, a routed model, a conversation, and a project. Everything else on the desktop is there when you want it.',
            'This tour lives in the dock if you ever want it back.',
        ],
        button: 'Close',
    },
];
