import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Render-error firewall. A throw anywhere below (a malformed tool payload,
 * partial markdown mid-stream, a Shiki edge case) becomes an inline notice
 * instead of unmounting the whole desktop — the exact blank-screen failure
 * mode the 0.4.6 TooltipProvider fix documented. Wrapped at four levels so
 * the blast radius shrinks with the crash site: each tool card, the reply
 * markdown, each message, and each pane.
 */
export class ErrorBoundary extends Component<
  {
    children: ReactNode;
    /** Custom fallback; receives a reset callback that re-attempts the render. */
    fallback?: (reset: () => void) => ReactNode;
    /** Names the crash site in the console (e.g. "tool:web_search"). */
    label?: string;
  },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surfaced to the browser console for diagnosis; never rethrown.
    console.error(
      `Circles render error${this.props.label ? ` [${this.props.label}]` : ""}:`,
      error,
      info,
    );
  }

  private reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback(this.reset);
      return (
        <div className="my-1 flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-destructive text-xs">
          <span className="min-w-0 break-words">
            Couldn’t render this part (
            {this.state.error.message || "render error"}).
          </span>
          <button
            className="shrink-0 underline underline-offset-2"
            onClick={this.reset}
            type="button"
          >
            retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
