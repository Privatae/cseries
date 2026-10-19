/**
 * Privatae Personal Computer — Viewport mode
 *
 * THE single definition of "phone mode" for the whole shell. Every
 * consumer (window manager, app runner, wallpaper) reads this instead
 * of rolling its own media query, so the breakpoint can never drift
 * between surfaces.
 *
 * Phone mode = a viewport at or under 768px (the standard phone/tablet
 * boundary), or a touch-primary device up to 1024px (portrait tablets:
 * wide enough for the desktop metaphor's chrome, too narrow for
 * 1000px+ app layouts). Viewport-based, not UA-sniffed — UA strings
 * lie, and a narrow desktop browser window deserves the same treatment.
 */

const CSeriesViewport = (() => {
    const mq = window.matchMedia(
        '(max-width: 768px), (pointer: coarse) and (max-width: 1024px)'
    );
    const listeners = new Set();

    // Older WebKit only has the deprecated addListener; feature-detect so
    // phone/desktop transitions (rotation, window resize) work everywhere.
    const notify = () => listeners.forEach((cb) => {
        try { cb(mq.matches); } catch (_) { /* one listener must not break the rest */ }
    });
    if (typeof mq.addEventListener === 'function') mq.addEventListener('change', notify);
    else if (typeof mq.addListener === 'function') mq.addListener(notify);

    return {
        isPhone: () => mq.matches,
        onChange: (cb) => { listeners.add(cb); },
    };
})();

window.CSeriesViewport = CSeriesViewport;
