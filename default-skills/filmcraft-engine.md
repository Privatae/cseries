---
name: filmcraft-engine
description: How to edit video with the FilmCraft Engine app — importing clips, building a sequence on the timeline, effects and titles, checking frames, and exporting the video as a job. Present when the person has installed FilmCraft Engine.
present_if_app_installed: filmcraft-engine
allowed-tools: mcp_connections, operate, image_generate, context
---

# FilmCraft Engine

FilmCraft Engine runs FilmCraft, an open-source video editor, headless in
the person's own space, for you. It is an MCP server named
`filmcraft-engine` (skill 'mcp', "An installed app's own server"):
connected when the app was installed, nothing to approve. One project stays
open in the app between calls.

## When to use

Cutting and assembling clips, titles and effects, a short video from images
or renders, and exporting it.

## First time

1. `mcp_connections(op='tools', name='filmcraft-engine')`: it wakes the app.
   The tools are FilmCraft's own (`command_list`, `command_run`,
   `command_batch`, `media_import`, `project_inspect`, `sequence_inspect`,
   `render_frame`, `render_preview`, …) and the app's `export`, `file_put`,
   `file_get`, `file_list` and `job_status`.
2. Write `skills/mcp-filmcraft-engine` yourself, to the standard (skill 'mcp'):
   `operate(tool='context', args={action:'write', path:'skills/mcp-filmcraft-engine', content:…})`.

## Steps

1. **A project:** `command_run(id: 'file.newProject', params: {name})`, or
   `file.openDemoProject` to learn the timeline on a ready one.
2. **Media in:** put a file into the app with `file_put` (an image of yours
   by its `/api/images/…` name, or a data: URL), then
   `media_import(paths: ['media/<file>'])` with the path you put it at; several
   at once in one list. `image_sequence: true` takes numbered stills as one
   clip.
3. **Edit:** every edit is an engine command; `command_list(filter='<word>')`
   gives ids and parameters (`sequence.addEdit`, `timeline.razor`,
   `effects.apply`, …). `sequence_inspect` returns the ids commands take.
4. **Look:** `render_preview(seconds)` returns one Program frame as an image,
   kept in your gallery; you see it right after the tool results and judge it
   yourself (later: `image_generate(action='look', images=['/api/images/…'])`).
5. **Export as a job:** `export(output: 'exports/<name>.mp4', start, end,
   scale)` saves the project, then exports the active sequence and answers
   with `{job}`. Wait with a watch on `job_status` with
   `while_text: '<id> running'`, the job's id first (`job-1a2b3c4d running`;
   skill 'mcp'), never a loop.

## Checks

- The export's job answer says `done`. A video does not come back to you as
  an image: it is in the app's window, where the person plays or downloads it.
- `file_list` shows the size of what was written.

## When it fails

- **The export failed**: its answer carries FilmCraft's own output. Check the
  sequence has clips with `sequence_inspect`.
- **Export time**: FilmCraft encodes on the CPU here. A 4-second 1080p H.264
  export took 6.6 s on two CPUs when measured; `scale: 0.5` makes quick drafts.
  Exports are jobs and queue one after another.

## Reference

FilmCraft is MIT or Apache-2.0 (github.com/storytold/filmcraft).
