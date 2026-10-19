// Visuals and artifact cards in chat — one copy for Circles and the Workbench.
// See visual.ts for what a visual is and how it differs from an artifact.
import { VisualBlock } from "./visual-block";
import { VISUAL_LANG } from "./visual";

export { ArtifactCard } from "./artifact-card";
export { artifactIdOf, artifactsOfTurn, type ArtifactToolLike } from "./artifact-ref";
export { openArtifactWindow, SealedFrame } from "./sealed-frame";
export { VisualBlock, VisualsAllowed } from "./visual-block";
export { isOwnHostTurn, VISUAL_LANG } from "./visual";

/** The Streamdown renderer entry: a ```visual fence draws as the page; every
 * other fence, plain ```html included, stays a code listing. */
export const visualRenderer = { language: VISUAL_LANG, component: VisualBlock };
