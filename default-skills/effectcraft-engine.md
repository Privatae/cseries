---
name: effectcraft-engine
description: How to make motion graphics with the EffectCraft Engine app — compositions, text, shape and solid layers, keyframes, effects, imported images, checking frames, and rendering video or image sequences. Present when the person has installed EffectCraft Engine.
present_if_app_installed: effectcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# EffectCraft Engine

EffectCraft Engine runs EffectCraft, an open-source compositor for animated
titles, motion graphics and compositing in the style of After Effects,
headless in the person's own space, for you. It is an MCP server named
`effectcraft-engine` (skill 'mcp', "An installed app's own server"):
connected when the app was installed, nothing to approve. One project stays
open in the app between calls.

## When to use

Animated titles and lower thirds, shapes and text that move, effects such as
glow, blur and drop shadow, and short clips built from the person's images or
images you generated (`image_generate`), rendered to MP4, WebM, GIF or a PNG
sequence.

## First time

1. `mcp_connections(op='tools', name='effectcraft-engine')`: it wakes the app.
   The tools are EffectCraft's own (`open_project`, `command_list`,
   `command_run`, `batch`, `add_keyframe`, `set_property`, `add_effect`,
   `list_effects`, `list_fonts`, `get_comp`, `get_layer`, `render_preview`,
   `render_frame`, `save_project`, `run_script`, …) and the app's `render`,
   `file_put`, `file_get`, `file_list` and `job_status`.
2. Write `skills/mcp-effectcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-effectcraft-engine', content:…})`.

## Steps

Paths are inside the app's files, written short (`renders/title.mp4`); the
folder a file goes into is made for you.

1. **A project:** `open_project(new: true)`, then
   `command_run(id: 'comp.new', params: {name: 'Main', width: 1920, height: 1080, frameRate: 30, duration: 5, background: '#101828'})`.
   Give `frameRate`: without it a comp is 29.97 fps.
2. **Layers:** `command_run` with `layer.newSolid {name, color}`,
   `layer.newShape {kind: 'ellipse'|'rect'|'star'|…, name, size: [w, h], fill, position: [x, y]}`
   or `layer.newText {text, name, size, fill, font, position}`. Each answers
   `{layer: id}`; later calls take the id or the layer's name. Fonts that are
   there: `list_fonts` (Inter, JetBrains Mono and Noto Serif are built in).
   `batch(steps: [...])` runs several commands as one undo step, a later step
   using an earlier one's result as `"$1.layer"`.
3. **Images in:** `file_put(path: 'in/<name>.png', data: '/api/images/…')`, then
   `command_run(id: 'file.import', params: {paths: ['in/<name>.png'], addToComp: true})`.
4. **Animate:** `add_keyframe(layer: 'Title', path: 'transform/position', keys: [{time: 0, value: [-300, 540]}, {time: 1.5, value: [960, 540]}], interpolation: 'easyEase')`.
   Times are seconds. `get_layer(layer)` lists every property `path`
   (`transform/scale`, `transform/rotation`, `transform/opacity`, …); a
   property path is not a file path.
5. **Effects:** `add_effect(layer, effect: 'Drop Shadow', values: {distance: 8, softness: 12})`.
   `list_effects(filter: '<word>')` finds one; the answer lists the effect's
   parameter paths.
6. **Scripts:** `run_script(code)` runs JavaScript with an After
   Effects-style object model (`app.project`, `comp.layers.addText`,
   `layer.property(...).setValueAtTime(t, v)`, expressions, markers), for
   work that is many similar edits. Every edit it makes is an undoable command.
7. **Look:** `render_preview(time: 1.5, max_side: 960)` returns that frame as an
   image, kept in your gallery. You see it
   right after the tool results and judge it yourself (later:
   `image_generate(action='look', images=['/api/images/…'])`).
8. **Save:** `save_project(path: 'projects/<name>.ecproj')`.
9. **Render as a job:** `render(output: 'renders/<name>.mp4', comp: 'Main')`
   saves the project, then renders it and answers with `{job}`; `start` and
   `end` (seconds) render part of it, `format` picks h264, hevc, av1, prores,
   webm, gif, png, jpeg, tiff, exr, wav or aiff (default: from the
   extension), and `resolution: 'half'` makes a quick draft. An image
   sequence: `output: 'renders/seq/frame.png'` writes `frame_00000.png`, … Wait
   with a watch on `job_status` with `while_text: '<id> running'`, the job's
   id first (`job-1a2b3c4d running`; skill 'mcp'), never a loop.
10. **A still:** `render_frame(time, max_side: 0, path: 'renders/<name>.png', inline: false)`
    writes a full-size PNG.

## Checks

- `get_comp` lists the layers with their in and out times; a keyframe answer
  carries a `warning` when a key falls outside the layer's time.
- The render's job answer says `done`. A video does not come back to you as
  an image: it is in the app's window, where the person plays or downloads
  it. `file_list` shows its size.

## When it fails

- **"unknown parameter(s)"**: the answer lists the parameters the command
  accepts (`file.import` takes `paths`, a list, not `path`).
- **The render failed**: its answer carries EffectCraft's own output. Check
  the comp's name with `get_project`.
- **Render time**: EffectCraft renders on the CPU here. 4 seconds of 1080p
  with six blurred shapes, fractal noise and a glow took 6 s on two CPUs and
  4 s on four when measured. Renders are jobs and queue one after another.

## Reference

EffectCraft is MIT or Apache-2.0 (github.com/storytold/effectcraft). It has
no generative features of its own: generate images with `image_generate`, then
animate them here. Projects are `.ecproj` (readable JSON); it cannot open
After Effects `.aep` files. It also reads and writes Lottie animations
(`file.exportLottie`).
