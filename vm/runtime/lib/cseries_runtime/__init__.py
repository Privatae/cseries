"""cseries-runtime: starts the containers a standalone C-Series Host asks for.

On a machine that runs a C-Series Host by itself (the Linux VM on a PC, or a
cloud box), nothing else starts the containers the Host's apps run in: the
messaging connectors, the creative engines, the browser and the coding
engines. This service does, with Podman, as root, on the Host's request over
a unix socket only the Host can reach. It speaks the five operations the Host
already sends for them (harness.wake, harness.release, harness.capacity,
workspace.set_harness, workspace.set_app_runtime) and answers in the same
shapes, so the Host's lanes work unchanged.

Python 3 standard library only. Every value from the Host is checked
(validate.py) before it reaches an argument list, and every argument list is
built from constants (argv.py): the Host names an app id, a family or an
engine id, never an image reference, a mount, a device or a flag.
"""
