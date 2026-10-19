---
name: blender-engine
description: How to make 3D scenes, wallpapers and artwork with the Blender Engine app — scripting a scene in bpy, rendering it on the CPU as a job, looking at the result and refining it. Present when the person has installed Blender Engine.
present_if_app_installed: blender-engine
allowed-tools: mcp_connections, operate, image_generate, context, artifact
---

# Blender Engine

Blender Engine runs Blender 5.2 headless in the person's own space, for you.
It is an MCP server named `blender-engine` (skill 'mcp', "An installed app's
own server"): connected when the app was installed, nothing to approve.

## When to use

The person wants a 3D scene, a rendered wallpaper or background, a product
shot, an abstract artwork, or anything a 3D renderer makes better than
drawing it.

## First time

1. `mcp_connections(op='tools', name='blender-engine')`: it wakes the app and
   lists `run_script`, `render`, `scene_info`, `file_put`, `file_get`,
   `file_list` and `job_status`.
2. Write `skills/mcp-blender-engine` yourself, to the standard (skill 'mcp'),
   with the tools you use in `allowed-tools`:
   `operate(tool='context', args={action:'write', path:'skills/mcp-blender-engine', content:…})`.
   Until it exists every call is refused.

## Making an image

1. **Write the scene as a bpy script.** Start empty
   (`bpy.ops.wm.read_factory_settings(use_empty=True)`), add the objects,
   materials, lights and a camera, and set `scene.camera`. Save it inside the
   app's files so later calls can open it; Blender makes no folders, so make
   it first (`os.makedirs('scenes', exist_ok=True)`, then
   `bpy.ops.wm.save_as_mainfile(filepath='scenes/<name>.blend')`). Write the render beside it with a `//` path,
   which Blender reads relative to the saved .blend, and name the format
   (Blender writes the format it is set to, not the one the name says):
   `scene.render.image_settings.file_format = 'JPEG'`,
   `scene.render.filepath = '//<name>-draft.jpg'`, then
   `bpy.ops.render.render(write_still=True)`.
2. **Iterate on drafts.** A first pass at a quarter of the size
   (`resolution_percentage = 25`) with 8 to 16 Cycles samples comes back in
   seconds; give the final pass the full size and as many samples as the
   image deserves.
3. **Run it as a job:** `operate(intent='<what you are making>',
   tool='mcp:blender-engine/run_script', args={code: '<script>',
   outputs: ['scenes/<name>-draft.jpg']})`. It answers with `{job}` at once.
4. **Wait with a watch, never a loop:** `operate(intent='waiting for the
   render', tool='mcp_connections', args={op: 'watch', tool:
   'mcp:blender-engine/job_status', args: {job: '<id>'}, while_text:
   '<id> running'})`, with the job's id in both places (`job-1a2b3c4d
   running`). The id makes the words the job's own: a file or a line of
   Blender's output may say "running" too.
5. **Look at it.** When the job is done, its image is kept in your gallery,
   the result names it (`/api/images/…`), and you see the picture yourself
   right after the tool results (if the result says it was not shown, look
   with `image_generate(action='look', images=['/api/images/…'])`). Judge it with your own eyes,
   change the script, and render again.
6. **The final render:** `render` opens a saved .blend and renders it at the
   size and samples you give (`engine: cycles` or `eevee`), up to 7680x4320.
   It comes back to you whole up to 20 MB (a 4K PNG often is more: ask for
   `.webp` or `.jpg` then); everything stays in the app's window as well.
   Jobs run one after another, so you can queue several renders at once.

## Checks

- The job's answer says `done` and carries the image. `failed` carries
  Blender's own output: the line with `Error` or the Python traceback says
  what to fix.
- `scene_info` lists a saved .blend's objects, camera and render settings.
- Everything the app made is in its window, newest first; the person can
  open and download any of it there.

## When it fails

- **The script raised.** Fix the line the traceback names. `bpy` operators
  that need a window (`bpy.ops.view3d.*`, `screen.*`) do not exist headless:
  set properties on the data instead.
- **Render time.** Blender renders on the CPU here: Cycles at 1920x1080 and
  64 samples took about 5 minutes on one CPU when measured. Denoising
  (`scene.cycles.use_denoising = True`) lets fewer samples look clean, and
  EEVEE is faster (give it lights and a world colour). A long render is a job:
  start it, watch it, and keep talking.

## Reference

- **A render as the wallpaper:** `operate(tool='artifact', args={op:'set_wallpaper',
  image_url:'/api/images/…'})` wears it (`variant: 'mobile'` for the phone).
  **In a desktop design or any artifact:** name it `/api/images/…` in a `src`
  or `url(...)` and the platform puts the image into the document (skill
  'desktop', rule 4): a rendered backdrop under a live desktop, textures,
  illustrations, icons.
- Blender is GPL-2.0-or-later; what it renders belongs to the person.
