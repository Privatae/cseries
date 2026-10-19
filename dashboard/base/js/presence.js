/**
 * C-Series — Ambient presence line
 *
 * Renders the entity's current activity as a third-person sentence,
 * bottom-right of the desktop. State comes from /api/entity/state —
 * signal-rule driven, zero model tokens.
 *
 * Poll on a slow interval. When signals push through the tunnel
 * later, /ws can emit an entity.state message to trigger immediate
 * refresh.
 */
const Presence = (() => {
    const POLL_MS = 8000;
    let lastActivity = null;
    let timer = null;

    function el() {
        return document.getElementById('presence-line');
    }

    async function refresh() {
        const node = el();
        if (!node) return;
        try {
            // read:session is gated (server/routes/entity.py:40), so this
            // carries the shell's own scoped token. A 401 means the kernel
            // secret rotated under us (container restart invalidates every
            // token) — re-mint once and retry, rather than polling a dead
            // token every 8s until the tab is reloaded.
            let r = await fetch('/api/entity/state', {
                credentials: 'same-origin',
                headers: ShellToken.headers(),
            });
            if (r.status === 401) {
                await ShellToken.retry();
                r = await fetch('/api/entity/state', {
                    credentials: 'same-origin',
                    headers: ShellToken.headers(),
                });
            }
            if (!r.ok) return;
            const s = await r.json();
            render(node, s);
        } catch (_) {
            // Silent — presence line is decorative; an error shouldn't
            // ever interrupt the user's work.
        }
    }

    function render(node, s) {
        const activity = s.activity || 'idle';
        const rawSentence = s.sentence || '';
        // Normalize ALL-CAPS activity text to natural sentence case.
        // If the sentence is entirely uppercase (e.g. "BACKING UP"),
        // lowercase it and capitalise the first letter.
        const sentence = rawSentence === rawSentence.toUpperCase() && rawSentence.length > 1
            ? rawSentence.charAt(0).toUpperCase() + rawSentence.slice(1).toLowerCase()
            : rawSentence;

        // Class swap: let CSS handle the transition.
        node.classList.remove('idle', 'offline', 'interactive');
        if (activity === 'idle') node.classList.add('idle');
        if (activity === 'offline') node.classList.add('offline');

        // Mark clickable activities — clicking jumps to the relevant app
        const clickTarget = clickTargetForActivity(activity);
        if (clickTarget) node.classList.add('interactive');

        // If the sentence changed, do a brief fade-in
        if (node.textContent !== sentence) {
            node.classList.remove('visible');
            // allow the class removal to register before we set new text
            setTimeout(() => {
                node.textContent = sentence;
                node.classList.add('visible');
            }, 60);
        } else if (!node.classList.contains('visible')) {
            node.classList.add('visible');
        }

        node.onclick = clickTarget
            ? () => {
                if (window.AppRunner) window.AppRunner.launch(clickTarget);
            }
            : null;

        lastActivity = activity;
    }

    function clickTargetForActivity(activity) {
        // Map activity → which app to open on click. Start narrow;
        // extend as more activity types land.
        if (activity === 'reading_gmail' || activity === 'drafting_gmail' || activity === 'sending_gmail') return 'portal';
        if (activity === 'reading_folder') return 'portal';
        if (activity === 'coding') return 'projects';  // future app
        if (activity === 'backing_up' || activity === 'restoring') return 'settings';
        if (activity === 'updating') return 'settings';
        return null;
    }

    function start() {
        refresh();
        timer = setInterval(refresh, POLL_MS);
    }

    function stop() {
        if (timer) clearInterval(timer);
        timer = null;
    }

    document.addEventListener('DOMContentLoaded', start);

    return { refresh, start, stop };
})();

window.Presence = Presence;
