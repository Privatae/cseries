"""
Code Harness — extract, write, execute, validate code from model output.

Handles every format models throw at us:
  - Fenced code blocks (```python ... ```)
  - Tool call format (Kimi's functions.code)
  - Inline XML code blocks (<code language="python">...</code>)
  - Raw code with language hints
  - Multi-file outputs with directory structure

Execution covers:
  - Python: syntax check → import check → run with captured output
  - SQL: psql execution against container PG
  - JavaScript/TypeScript: node --check syntax, then run
  - HTML/CSS: validate structure, write to serve path
  - Bash/Shell: syntax check → execute
  - YAML/TOML/JSON: parse validation
  - Dockerfile: syntax lint
  - FastAPI apps: syntax + import + uvicorn dry-run

Sandbox: all execution in /var/lib/cseries/projects/{id}/, no access outside
the Workbench project directory.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("privatae.pc.harness")

# Moved from /app/projects (image-baked, wiped on
# pod restart, shared across entities — the same leak class once fixed
# for local_grep) to /var/lib/cseries/projects (per-entity persistent
# volume).
WORKSPACE_ROOT = Path("/var/lib/cseries/projects")

# Language → file extension mapping
LANG_EXT = {
    "python": ".py", "py": ".py", "python3": ".py",
    "sql": ".sql", "postgresql": ".sql", "psql": ".sql",
    "html": ".html", "htm": ".html",
    "css": ".css",
    "javascript": ".js", "js": ".js",
    "typescript": ".ts", "ts": ".ts",
    "jsx": ".jsx", "tsx": ".tsx",
    "bash": ".sh", "sh": ".sh", "shell": ".sh", "zsh": ".sh",
    "json": ".json",
    "yaml": ".yml", "yml": ".yml",
    "toml": ".toml",
    "dockerfile": ".dockerfile",
    "docker": ".dockerfile",
    "markdown": ".md", "md": ".md",
    "xml": ".xml",
    "csv": ".csv",
    "ini": ".ini", "conf": ".conf",
    "nginx": ".conf",
    "env": ".env",
    "txt": ".txt", "text": ".txt",
    "requirements": ".txt",
}

# ── Extraction ────────────────────────────────────────

def extract_code_blocks(text):
    """Extract code from model output in any format.

    Handles: fenced blocks, tool calls, XML code blocks, indented blocks.
    Returns list of {language, filename, content}.
    """
    blocks = []

    # 1. Fenced code blocks: ```lang\n...\n```
    blocks.extend(_extract_fenced(text))

    # 2. Kimi/OpenAI tool call format: functions.code with JSON args
    blocks.extend(_extract_tool_calls(text))

    # 3. XML code blocks: <code language="python">...</code>
    blocks.extend(_extract_xml_code(text))

    # 4. Artifact blocks: <artifact type="code" language="python">...</artifact>
    blocks.extend(_extract_artifacts(text))

    # 5. Anthropic tool_use content blocks: {"type":"tool_use","name":"...","input":{...}}
    blocks.extend(_extract_anthropic_tool_use(text))

    # 6. Gemini function calls: {"functionCall":{"name":"...","args":{...}}}
    blocks.extend(_extract_gemini_function_calls(text))

    # 7. XML tool invocations: <invoke name="bash"><parameter name="command">...</parameter></invoke>
    blocks.extend(_extract_invoke_heredocs(text))

    # 8. Heredocs in bash: cat > filename << 'EOF'\n...\nEOF
    blocks.extend(_extract_bare_heredocs(text))

    # Deduplicate by content hash
    seen = set()
    unique = []
    for b in blocks:
        h = hash(b["content"][:500])
        if h not in seen:
            seen.add(h)
            unique.append(b)

    # Assign filenames where missing, handling multiple files of same type
    type_counts = {}
    for b in unique:
        if not b.get("filename"):
            lang = b["language"]
            type_counts[lang] = type_counts.get(lang, 0) + 1
            count = type_counts[lang]
            ext = LANG_EXT.get(lang, ".txt")
            b["filename"] = f"output_{count}{ext}" if count > 1 else f"output{ext}"

    return unique


def _extract_fenced(text):
    """Extract ```lang ... ``` blocks."""
    blocks = []
    pattern = re.compile(r"```(\w[\w+\-.]*)?\s*\n(.*?)```", re.DOTALL)

    for match in pattern.finditer(text):
        lang = (match.group(1) or "").lower().strip()
        content = match.group(2).strip()
        if not content:
            continue

        filename = _detect_filename(content, lang)
        blocks.append({"language": lang or "text", "filename": filename, "content": content})

    return blocks


def _extract_tool_calls(text):
    """Extract Kimi/OpenAI tool call format."""
    blocks = []

    # Kimi format: <|tool_call_begin|>functions.code:0<|tool_call_argument_begin|>{"language":"python","code":"..."}
    tc_pattern = re.compile(
        r'(?:tool_call_argument_begin\|>|"arguments"\s*:\s*)\s*(\{[^}]*"(?:code|content)"[^}]*\})',
        re.DOTALL,
    )
    for match in tc_pattern.finditer(text):
        try:
            args = json.loads(match.group(1))
            code = args.get("code") or args.get("content") or ""
            lang = args.get("language", "python").lower()
            if code.strip():
                blocks.append({"language": lang, "filename": None, "content": code.strip()})
        except json.JSONDecodeError:
            pass

    # Also try parsing the full tool_calls JSON array
    tc_array = re.compile(r'"tool_calls"\s*:\s*\[([^\]]+)\]', re.DOTALL)
    for match in tc_array.finditer(text):
        try:
            calls = json.loads(f"[{match.group(1)}]")
            for call in calls:
                fn = call.get("function", {})
                if fn.get("name") in ("code", "write_file", "create_file"):
                    args = json.loads(fn.get("arguments", "{}"))
                    code = args.get("code") or args.get("content") or ""
                    lang = args.get("language", "python").lower()
                    fname = args.get("filename") or args.get("path")
                    if code.strip():
                        blocks.append({"language": lang, "filename": fname, "content": code.strip()})
        except (json.JSONDecodeError, TypeError):
            pass

    return blocks


def _extract_xml_code(text):
    """Extract <code language="...">...</code> blocks."""
    blocks = []
    pattern = re.compile(r'<code\s+(?:language|lang)\s*=\s*["\'](\w+)["\']\s*>(.*?)</code>', re.DOTALL)
    for match in pattern.finditer(text):
        lang = match.group(1).lower()
        content = match.group(2).strip()
        if content:
            blocks.append({"language": lang, "filename": None, "content": content})
    return blocks


def _extract_artifacts(text):
    """Extract <artifact> blocks (Claude-style)."""
    blocks = []
    pattern = re.compile(
        r'<artifact[^>]*(?:language|lang)\s*=\s*["\'](\w+)["\'][^>]*>(.*?)</artifact>',
        re.DOTALL,
    )
    for match in pattern.finditer(text):
        lang = match.group(1).lower()
        content = match.group(2).strip()
        if content:
            blocks.append({"language": lang, "filename": None, "content": content})
    return blocks


def _extract_anthropic_tool_use(text):
    """Extract Anthropic tool_use content blocks.

    Format: {"type":"tool_use","id":"...","name":"write_file","input":{"path":"app.py","content":"..."}}
    Also handles: {"type":"tool_use","name":"bash","input":{"command":"cat > file..."}}
    """
    blocks = []
    # Find tool_use JSON objects
    pattern = re.compile(r'\{"type"\s*:\s*"tool_use"[^}]*"name"\s*:\s*"(\w+)"[^}]*"input"\s*:\s*(\{[^}]+\})', re.DOTALL)
    for match in pattern.finditer(text):
        name = match.group(1)
        try:
            inp = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue

        if name in ("write_file", "create_file", "str_replace_editor"):
            content = inp.get("content") or inp.get("new_str") or ""
            path = inp.get("path") or inp.get("file_path") or ""
            if content.strip():
                lang = _lang_from_filename(path) if path else "text"
                blocks.append({"language": lang, "filename": os.path.basename(path) if path else None, "content": content.strip()})

        elif name == "bash" and inp.get("command"):
            blocks.extend(_extract_bare_heredocs(inp["command"]))

    return blocks


def _extract_gemini_function_calls(text):
    """Extract Gemini functionCall format.

    Format: {"functionCall":{"name":"write_code","args":{"language":"python","code":"..."}}}
    """
    blocks = []
    pattern = re.compile(r'"functionCall"\s*:\s*\{[^}]*"name"\s*:\s*"(\w+)"[^}]*"args"\s*:\s*(\{[^}]+\})', re.DOTALL)
    for match in pattern.finditer(text):
        name = match.group(1)
        try:
            args = json.loads(match.group(2))
        except json.JSONDecodeError:
            continue

        code = args.get("code") or args.get("content") or args.get("file_content") or ""
        lang = args.get("language", "text").lower()
        fname = args.get("filename") or args.get("path")
        if code.strip():
            blocks.append({"language": lang, "filename": fname, "content": code.strip()})

    return blocks


def _extract_invoke_heredocs(text):
    """Extract code from XML tool invocations with heredocs.

    Handles patterns like:
      <invoke name="bash"><parameter name="command">
      cat > schema.sql << 'EOF'
      CREATE TABLE ...
      EOF
      </parameter></invoke>
    """
    blocks = []

    # Find <parameter name="command">...</parameter> blocks
    param_pattern = re.compile(
        r'<parameter\s+name\s*=\s*["\']command["\']\s*>(.*?)</parameter>',
        re.DOTALL,
    )
    for param_match in param_pattern.finditer(text):
        command = param_match.group(1).strip()
        # Extract heredocs from the command
        blocks.extend(_extract_bare_heredocs(command))

        # Also check for inline file writes: echo '...' > filename
        echo_pattern = re.compile(r"echo\s+'([^']+)'\s*>\s*(\S+)")
        for em in echo_pattern.finditer(command):
            content = em.group(1)
            fname = em.group(2)
            lang = _lang_from_filename(fname)
            blocks.append({"language": lang, "filename": fname, "content": content})

    return blocks


def _extract_bare_heredocs(text):
    """Extract heredocs: cat > filename << 'DELIM'\n...\nDELIM"""
    blocks = []

    heredoc_pattern = re.compile(
        r"cat\s*>\s*(\S+)\s*<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\2",
        re.DOTALL,
    )
    for match in heredoc_pattern.finditer(text):
        fname = match.group(1)
        content = match.group(3).strip()
        lang = _lang_from_filename(fname)
        if content:
            blocks.append({"language": lang, "filename": fname, "content": content})

    # Also handle: tee filename << 'DELIM'
    tee_pattern = re.compile(
        r"tee\s+(\S+)\s*<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\2",
        re.DOTALL,
    )
    for match in tee_pattern.finditer(text):
        fname = match.group(1)
        content = match.group(3).strip()
        lang = _lang_from_filename(fname)
        if content:
            blocks.append({"language": lang, "filename": fname, "content": content})

    return blocks


def _lang_from_filename(filename):
    """Infer language from filename extension."""
    ext = os.path.splitext(filename)[1].lower()
    ext_to_lang = {v: k for k, v in LANG_EXT.items()}
    # Resolve duplicates
    ext_to_lang.update({
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".sql": "sql", ".html": "html", ".css": "css", ".sh": "bash",
        ".json": "json", ".yml": "yaml", ".toml": "toml", ".md": "markdown",
    })
    return ext_to_lang.get(ext, "text")


def _detect_filename(content, lang):
    """Try to detect filename from code content."""
    first_line = content.split("\n")[0] if content else ""

    # Comment-style filename hints
    patterns = [
        r"^#\s*(?:file(?:name)?:\s*)?(\S+\.\w+)",        # # filename.py or # file: app.py
        r"^--\s*(?:file(?:name)?:\s*)?(\S+\.\w+)",        # -- schema.sql
        r"^//\s*(?:file(?:name)?:\s*)?(\S+\.\w+)",        # // index.js
        r"^/\*\s*(?:file(?:name)?:\s*)?(\S+\.\w+)",       # /* styles.css
        r"^<!--\s*(?:file(?:name)?:\s*)?(\S+\.\w+)",      # <!-- index.html
        r'^"""\s*(\S+\.\w+)',                               # """app.py
    ]
    for p in patterns:
        m = re.match(p, first_line)
        if m:
            return m.group(1)

    # Shebang detection
    if first_line.startswith("#!/"):
        if "python" in first_line:
            return None  # let auto-name handle it
        if "bash" in first_line or "sh" in first_line:
            return None

    # FastAPI/Flask detection → app.py
    if lang in ("python", "py"):
        if "FastAPI(" in content or "from fastapi" in content.lower():
            return "app.py"
        if "Flask(" in content or "from flask" in content.lower():
            return "app.py"
        if "if __name__" in content:
            return "main.py"

    # HTML with <title> → use title
    if lang in ("html", "htm"):
        title_match = re.search(r"<title>([^<]+)</title>", content, re.IGNORECASE)
        if title_match:
            safe = re.sub(r"[^\w\-]", "_", title_match.group(1).strip().lower())[:30]
            return f"{safe}.html"
        return "index.html"

    return None


# ── File Writing ──────────────────────────────────────

async def write_files(project_id, blocks, step_id=None):
    """Write code blocks to project workspace. Returns list of written file info."""
    workspace = WORKSPACE_ROOT / str(project_id)
    if step_id:
        workspace = workspace / f"step_{step_id}"
    workspace.mkdir(parents=True, exist_ok=True)

    written = []
    seen_names = set()
    for i, block in enumerate(blocks):
        filename = block.get("filename") or f"block_{i}{LANG_EXT.get(block['language'], '.txt')}"

        # Deduplicate filenames within a step
        if filename in seen_names:
            base, ext = os.path.splitext(filename)
            filename = f"{base}_{i}{ext}"
        seen_names.add(filename)

        # Handle subdirectory paths in filename
        filepath = workspace / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(block["content"])

        written.append({
            "filename": filename,
            "path": str(filepath),
            "language": block["language"],
            "size": len(block["content"]),
        })
        logger.info("Wrote %s (%d bytes)", filepath, len(block["content"]))

    return written


# ── Execution ─────────────────────────────────────────

async def execute_file(filepath, language, timeout=30, workspace=None):
    """Execute a file and return stdout, stderr, exit code, phase."""
    filepath = str(filepath)
    cwd = workspace or str(Path(filepath).parent)

    if language in ("python", "py", "python3"):
        return await _execute_python(filepath, timeout, cwd)
    elif language in ("sql", "postgresql", "psql"):
        return await _execute_sql(filepath, timeout)
    elif language in ("javascript", "js"):
        return await _execute_js(filepath, timeout, cwd)
    elif language in ("typescript", "ts"):
        return await _execute_ts(filepath, timeout, cwd)
    elif language in ("bash", "sh", "shell", "zsh"):
        return await _execute_shell(filepath, timeout, cwd)
    elif language == "json":
        return _validate_json(filepath)
    elif language in ("yaml", "yml"):
        return _validate_yaml(filepath)
    elif language == "toml":
        return _validate_toml(filepath)
    elif language in ("html", "htm"):
        return _validate_html(filepath)
    elif language == "css":
        return _validate_css(filepath)
    elif language in ("dockerfile", "docker"):
        return await _validate_dockerfile(filepath, timeout)
    elif language in ("xml",):
        return _validate_xml(filepath)
    elif language in ("markdown", "md", "txt", "text", "env", "ini", "conf", "csv", "nginx", "requirements"):
        return {"stdout": f"static file ({language})", "stderr": "", "exit_code": 0, "phase": "written"}
    else:
        return {"stdout": "", "stderr": f"no executor for: {language}", "exit_code": 0, "phase": "skipped"}


async def _execute_python(filepath, timeout, cwd):
    """Python: syntax check → import check → execute."""
    # Syntax check
    result = await _run(["python3", "-m", "py_compile", filepath], timeout, cwd)
    if result["exit_code"] != 0:
        return {**result, "phase": "syntax_check"}

    # Check for FastAPI/Flask apps — don't run servers, just validate imports
    content = Path(filepath).read_text()
    is_server = any(k in content for k in ["uvicorn.run", "app.run(", ".run(host=", "serve_forever"])

    if is_server:
        # Import-only check for server apps
        module_name = Path(filepath).stem
        check_code = f"import importlib.util; spec = importlib.util.spec_from_file_location('{module_name}', '{filepath}'); mod = importlib.util.module_from_spec(spec)"
        result = await _run(["python3", "-c", check_code], timeout, cwd)
        return {**result, "phase": "import_check"}

    # Full execution for scripts
    result = await _run(["python3", filepath], timeout, cwd)
    return {**result, "phase": "execution"}


async def _execute_sql(filepath, timeout):
    """SQL: run against container PostgreSQL. Use --set ON_ERROR_STOP=1 for strict mode.

    The database name is NOT hardcoded: entrypoint.sh resolves it at boot
    (`hostmem`, or the legacy `parallax` on a pod whose rename has not happened)
    and exports ENTITY_DB into this process. The default is the new name, which
    is what any cluster this image creates is called.
    """
    result = await _run(
        ["psql", "-U", "entity", "-d", os.environ.get("ENTITY_DB", "hostmem"),
         "-h", "/var/run/postgresql",
         "--set", "ON_ERROR_STOP=1", "-f", filepath],
        timeout,
    )
    return {**result, "phase": "sql_execution"}


async def _execute_js(filepath, timeout, cwd):
    """JavaScript: node --check syntax always. Execute via `node` only if
    the file is clearly a node-runnable script (no browser globals, no
    server bind, no ESM markers).

    The earlier heuristic only skipped execution for explicit server code
    (.listen / createServer / express). That left browser-side scripts —
    anything touching `document`, `window`, `localStorage`, or any ESM
    file — getting `node app.js`'d, which crashes immediately. The agent
    was working around the validator by producing CommonJS-shaped code
    even when the project wanted ESM (see agent comment about "passes a
    CommonJS-style syntax check"). That's the validator distorting the
    agent's output. Fix: syntax check is enough for those classes.
    """
    result = await _run(["node", "--check", filepath], timeout, cwd)
    if result["exit_code"] != 0:
        return {**result, "phase": "syntax_check"}

    content = Path(filepath).read_text()

    # Server: starts a listener, don't try to execute (would hang).
    is_server = any(k in content for k in [".listen(", "createServer", "express()"])
    if is_server:
        return {**result, "phase": "syntax_check (server, not executed)"}

    # Browser: uses DOM/BOM globals that don't exist in node.
    browser_globals = (
        "document.", "window.", "navigator.", "localStorage",
        "sessionStorage", "DOMContentLoaded", "HTMLElement",
        "querySelector", "getElementById",
    )
    is_browser = any(g in content for g in browser_globals)
    if is_browser:
        return {**result, "phase": "syntax_check (browser, not executed)"}

    # ES module: static `import x from 'y'` / `export ...` at module scope.
    # `node <file>.js` runs as CommonJS by default and would throw
    # `SyntaxError: Cannot use import statement outside a module`.
    # Detect line-anchored top-level import/export.
    is_esm = bool(re.search(r"^\s*(import\s+[\w*{},\s]+from\s+['\"]|export\s+(default|const|let|var|function|class|\{))", content, re.MULTILINE))
    if is_esm:
        return {**result, "phase": "syntax_check (ES module, not executed)"}

    result = await _run(["node", filepath], timeout, cwd)
    return {**result, "phase": "execution"}


async def _execute_ts(filepath, timeout, cwd):
    """TypeScript: try tsx/ts-node, fall back to syntax heuristic."""
    for runner in ["tsx", "ts-node", "npx tsx"]:
        which = await _run(["which", runner.split()[0]], 5)
        if which["exit_code"] == 0:
            result = await _run([*runner.split(), filepath], timeout, cwd)
            return {**result, "phase": "execution"}
    return {"stdout": "no TypeScript runner available", "stderr": "", "exit_code": 0, "phase": "skipped"}


async def _execute_shell(filepath, timeout, cwd):
    """Shell: syntax check then execute."""
    result = await _run(["bash", "-n", filepath], timeout, cwd)
    if result["exit_code"] != 0:
        return {**result, "phase": "syntax_check"}
    result = await _run(["bash", filepath], timeout, cwd)
    return {**result, "phase": "execution"}


def _validate_json(filepath):
    try:
        json.loads(Path(filepath).read_text())
        return {"stdout": "valid JSON", "stderr": "", "exit_code": 0, "phase": "validation"}
    except json.JSONDecodeError as e:
        return {"stdout": "", "stderr": str(e), "exit_code": 1, "phase": "validation"}


def _validate_yaml(filepath):
    try:
        import yaml
        yaml.safe_load(Path(filepath).read_text())
        return {"stdout": "valid YAML", "stderr": "", "exit_code": 0, "phase": "validation"}
    except ImportError:
        return {"stdout": "yaml module not available", "stderr": "", "exit_code": 0, "phase": "skipped"}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": 1, "phase": "validation"}


def _validate_toml(filepath):
    try:
        import tomllib
        with open(filepath, "rb") as f:
            tomllib.load(f)
        return {"stdout": "valid TOML", "stderr": "", "exit_code": 0, "phase": "validation"}
    except ImportError:
        try:
            import tomli
            with open(filepath, "rb") as f:
                tomli.load(f)
            return {"stdout": "valid TOML", "stderr": "", "exit_code": 0, "phase": "validation"}
        except ImportError:
            return {"stdout": "toml parser not available", "stderr": "", "exit_code": 0, "phase": "skipped"}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": 1, "phase": "validation"}


def _validate_html(filepath):
    """Basic HTML validation — check for matching tags, valid structure."""
    content = Path(filepath).read_text()
    issues = []
    if "<html" not in content.lower() and "<!doctype" not in content.lower():
        issues.append("missing <html> or <!DOCTYPE>")
    if "<head" not in content.lower():
        issues.append("missing <head>")
    if "<body" not in content.lower() and "<template" not in content.lower():
        issues.append("missing <body>")

    # Check for unclosed script/style tags
    for tag in ("script", "style"):
        opens = len(re.findall(f"<{tag}[^>]*>", content, re.IGNORECASE))
        closes = len(re.findall(f"</{tag}>", content, re.IGNORECASE))
        if opens != closes:
            issues.append(f"mismatched <{tag}> tags ({opens} open, {closes} close)")

    if issues:
        return {"stdout": "", "stderr": "; ".join(issues), "exit_code": 1, "phase": "validation"}
    return {"stdout": f"valid HTML ({len(content)} chars)", "stderr": "", "exit_code": 0, "phase": "validation"}


def _validate_css(filepath):
    content = Path(filepath).read_text()
    opens = content.count("{")
    closes = content.count("}")
    if opens != closes:
        return {"stdout": "", "stderr": f"mismatched braces ({opens} open, {closes} close)", "exit_code": 1, "phase": "validation"}
    return {"stdout": f"valid CSS ({len(content)} chars)", "stderr": "", "exit_code": 0, "phase": "validation"}


def _validate_xml(filepath):
    try:
        import xml.etree.ElementTree as ET
        ET.parse(filepath)
        return {"stdout": "valid XML", "stderr": "", "exit_code": 0, "phase": "validation"}
    except ET.ParseError as e:
        return {"stdout": "", "stderr": str(e), "exit_code": 1, "phase": "validation"}


async def _validate_dockerfile(filepath, timeout):
    which = await _run(["which", "hadolint"], 5)
    if which["exit_code"] == 0:
        result = await _run(["hadolint", filepath], timeout)
        return {**result, "phase": "lint"}
    # Basic check — every FROM has an image
    content = Path(filepath).read_text()
    if not re.search(r"^FROM\s+\S+", content, re.MULTILINE):
        return {"stdout": "", "stderr": "Dockerfile missing FROM instruction", "exit_code": 1, "phase": "validation"}
    return {"stdout": "basic Dockerfile structure OK", "stderr": "", "exit_code": 0, "phase": "validation"}


# ── Subprocess runner ─────────────────────────────────

async def _run(cmd, timeout=30, cwd=None):
    """Run subprocess, capture output, enforce timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "stdout": stdout.decode("utf-8", errors="replace")[:10000],
            "stderr": stderr.decode("utf-8", errors="replace")[:10000],
            "exit_code": proc.returncode,
        }
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except:
            pass
        return {"stdout": "", "stderr": f"timed out after {timeout}s", "exit_code": -1}
    except FileNotFoundError:
        return {"stdout": "", "stderr": f"command not found: {cmd[0]}", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


# ── Full pipeline ─────────────────────────────────────

async def execute_and_validate(project_id, model_output, step_id=None):
    """Extract → write → execute → report.

    Returns {files_written, execution_results, all_passed, errors_for_model}.
    """
    blocks = extract_code_blocks(model_output)
    if not blocks:
        return {
            "files_written": [],
            "execution_results": [],
            "all_passed": True,
            "errors_for_model": None,
        }

    written = await write_files(project_id, blocks, step_id=step_id)
    workspace = str(WORKSPACE_ROOT / str(project_id) / (f"step_{step_id}" if step_id else ""))

    results = []
    errors = []
    for w in written:
        exec_result = await execute_file(w["path"], w["language"], workspace=workspace)
        passed = exec_result["exit_code"] == 0
        results.append({
            "filename": w["filename"],
            "language": w["language"],
            "size": w["size"],
            "passed": passed,
            **exec_result,
        })
        if not passed:
            errors.append(
                f"File: {w['filename']} ({w['language']})\n"
                f"Phase: {exec_result.get('phase', 'unknown')}\n"
                f"Exit code: {exec_result['exit_code']}\n"
                f"stderr:\n{exec_result['stderr']}\n"
                f"stdout:\n{exec_result['stdout']}"
            )

    all_passed = all(r["passed"] for r in results)
    errors_for_model = "\n\n---\n\n".join(errors) if errors else None

    return {
        "files_written": written,
        "execution_results": results,
        "all_passed": all_passed,
        "errors_for_model": errors_for_model,
    }
