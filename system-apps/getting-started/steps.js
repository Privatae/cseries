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
 * ================================================================ */

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
        // Reached two ways: in order after the Orchestrator, and jumped to
        // directly from the Orchestrator's Memory step ("Run it free on your
        // machine" → AppRunner.launch('getting-started', {view:'network'})).
        // That button is why this slide exists: it promised a walkthrough of
        // the bastion and there was nowhere for it to land.
        id: 'network',
        appId: 'network',
        appName: 'Network',
        title: 'Give your host a machine of its own (optional)',
        lines: [
            'A bastion is just a computer you own — a spare laptop, a desktop, a box in a cupboard — that your host is allowed to use. It joins your private network and offers up whatever it can run.',
            'The useful thing it runs first is an embeddings model: the thing that lets your host recall what you meant, not just what you typed. On your own machine that costs nothing and the text never leaves your network.',
            'Open Network, follow "Bastions" to add a machine, and turn on "Serve my embeddings here". Skip this if you\'d rather use a provider — the Orchestrator can do that instead.',
        ],
        button: 'Open Network',
        check: {
            // An embed-capable machine is ONLINE and reporting. This is the
            // slide's actual goal; a bastion enrolled but serving nothing
            // hasn't got the user anywhere, so it must not pass.
            endpoint: '/api/embedding/bastions',
            evaluate: (d) => ((d && d.bastions) || []).length > 0,
            notYet: 'No machine reporting yet. Open Network again and finish adding one — or skip; a provider works too.',
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
