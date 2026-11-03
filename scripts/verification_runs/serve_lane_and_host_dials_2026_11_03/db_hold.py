"""Hold a throwaway cseries-postgres with the kernel schema until STOP appears; write the DSN to DSN."""
import os, sys, time, pathlib
root = sys.argv[1]; out = pathlib.Path(sys.argv[2])
sys.path[:0] = [root, os.path.join(root, "scripts")]
from kernel_schema import throwaway_server, kernel_schema_db
with throwaway_server() as srv, kernel_schema_db(srv, entity_id=1) as dsn:
    (out / "DSN").write_text(dsn + "\n")
    print("DSN", dsn, flush=True)
    while not (out / "STOP").exists():
        time.sleep(1)
print("stopped", flush=True)
