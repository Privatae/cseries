"""What gives a C-Series Host more room on its own machine.

The local runtime (vm/runtime guard.py) refuses a container with scope
"machine" when the Host's machine has no room for it. On a C-Series install
that room is the Host's size, which the person chose (the installer's
`npx cseries --size C4-8`) and can change, its data kept, with
`npx cseries resize`, run on that machine. The Host cannot change it itself:
the size is the VM's (or, on Windows, the ceiling inside its WSL system),
set from outside it. So every sentence that says this machine had no room
says how to give it more, as a plan's says which plan has room.

Asking for it with the approval pop-up (the Host asks, the person taps
their passkey, the machine resizes) needs an operation the bastion runs on
the machine; until there is one, the words name the command.
"""

# Inside a sentence, after the numbers.
RESIZE_CLAUSE = "npx cseries resize, run on this machine, gives C-Series more of it"
# A sentence of its own.
RESIZE_SENTENCE = "Running npx cseries resize on this machine gives C-Series more of it."
