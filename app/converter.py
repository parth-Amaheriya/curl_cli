"""
Curl to Python requests code converter
Refactored from user-provided logic for FastAPI integration
"""
import re
import shlex
import json
from urllib.parse import parse_qsl, urlparse
from typing import Dict, List, Optional, Any, Tuple,Union

# Regex for {{place}}, ${place}, <place>
PLACEHOLDER_PATTERN = re.compile(
    r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}|"
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|"
    r"<([A-Za-z_][A-Za-z0-9_]*)>"
)


def curl_to_requests(curl_command: str) -> Dict[str, Any]:
    """Parse curl command and extract request components"""
    tokens = shlex.split(curl_command)
    method = "GET"
    url = ""
    params = {}
    headers = {}
    data = None
    explicit_method = False

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "curl":
            i += 1
            continue

        # Skip flags that don't affect request structure
        if token in (
            "--location", "-L", "--compressed", "-s", "--silent",
            "--show-error", "--fail", "--fail-with-body", "--include", "-i"
        ):
            i += 1
            continue

        if token in ("--request", "-X") and i + 1 < len(tokens):
            method = tokens[i + 1].upper()
            explicit_method = True
            i += 2
            continue

        if token in ("--header", "-H") and i + 1 < len(tokens):
            header = tokens[i + 1]
            if ":" in header:
                key, value = header.split(":", 1)
                headers[key.strip()] = value.strip()
            i += 2
            continue

        if token in ("--cookie", "-b") and i + 1 < len(tokens):
            cookie_value = tokens[i + 1]
            if "Cookie" in headers and headers["Cookie"]:
                headers["Cookie"] += "; " + cookie_value
            else:
                headers["Cookie"] = cookie_value
            i += 2
            continue

        if token in ("--user-agent", "-A") and i + 1 < len(tokens):
            headers["User-Agent"] = tokens[i + 1]
            i += 2
            continue

        if token in (
            "--data", "--data-raw", "--data-binary",
            "--data-ascii", "--data-urlencode", "-d"
        ) and i + 1 < len(tokens):
            data = tokens[i + 1]
            if not explicit_method:
                method = "POST"
            i += 2
            continue

        if token in ("--get", "-G"):
            method = "GET"
            explicit_method = True
            i += 1
            continue

        if token.startswith("http://") or token.startswith("https://"):
            parsed_url = urlparse(token)
            url = parsed_url._replace(query="", fragment="").geturl()
            params = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
            i += 1
            continue

        i += 1

    return {
        "method": method,
        "url": url,
        "params": params,
        "headers": headers,
        "data": data,
    }


def find_placeholders(text: Optional[str]) -> List[str]:
    """Extract placeholder names from text"""
    if not text:
        return []
    found = []
    for match in PLACEHOLDER_PATTERN.finditer(text):
        name = next(group for group in match.groups() if group)
        if name not in found:
            found.append(name)
    return found


def sanitize_identifier(text: str) -> str:
    """Convert text to valid Python identifier"""
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        s = "value"
    if not re.match(r"[A-Za-z_]", s):
        s = f"param_{s}"
    return s


def render_template_expression(text: Optional[str]) -> str:
    """Render placeholder expressions for Python code"""
    if text is None:
        return "None"
    pieces = []
    last = 0
    for match in PLACEHOLDER_PATTERN.finditer(text):
        literal = text[last:match.start()]
        if literal:
            pieces.append(repr(literal))
        name = next(group for group in match.groups() if group)
        pieces.append(f"(str({name}) if {name} is not None else '')")
        last = match.end()
    tail = text[last:]
    if tail:
        pieces.append(repr(tail))
    if not pieces:
        return "''"
    if len(pieces) == 1:
        return pieces[0]
    return " + ".join(pieces)


def build_request_arguments(spec: Dict[str, Any]) -> Tuple:
    """Build function arguments from request spec"""
    arguments = []
    used_names = set()
    query_entries = []
    payload_entries = []
    payload_mode = None
    payload_literal = None

    def add_optional(name: str, default: Any) -> str:
        candidate = sanitize_identifier(name)
        if candidate in used_names:
            return candidate
        used_names.add(candidate)
        arguments.append({
            "name": candidate,
            "default": default,
            "required": False,
        })
        return candidate

    # Placeholders in URL
    for ph in find_placeholders(spec["url"]):
        add_optional(ph, None)

    # Placeholders in query params
    for key, value in spec["params"].items():
        phs = find_placeholders(value)
        if phs:
            for ph in phs:
                add_optional(ph, None)
            query_entries.append({
                "key": key,
                "expression": render_template_expression(value),
            })
        else:
            arg_name = add_optional(key, value)
            query_entries.append({
                "key": key,
                "expression": arg_name,
            })

    # Headers
    for val in spec["headers"].values():
        for ph in find_placeholders(val):
            add_optional(ph, None)

    # Body/payload
    if spec["data"]:
        body_text = spec["data"].strip()
        try:
            parsed_body = json.loads(body_text)
        except Exception:
            parsed_body = None

        if isinstance(parsed_body, dict):
            payload_mode = "json"
            for key, value in parsed_body.items():
                if isinstance(value, str):
                    phs = find_placeholders(value)
                    if phs:
                        for ph in phs:
                            add_optional(ph, None)
                        payload_entries.append({
                            "key": key,
                            "expression": render_template_expression(value),
                        })
                    else:
                        arg_name = add_optional(key, value)
                        payload_entries.append({
                            "key": key,
                            "expression": arg_name,
                        })
                else:
                    arg_name = add_optional(key, value)
                    payload_entries.append({
                        "key": key,
                        "expression": arg_name,
                    })
        else:
            payload_mode = "raw"
            payload_literal = render_template_expression(spec["data"])
            for ph in find_placeholders(spec["data"]):
                add_optional(ph, None)

    return arguments, query_entries, payload_entries, payload_mode, payload_literal


def render_request_function(spec: Dict[str, Any]) -> str:
    """Generate Python function code from spec"""
    args = []
    for arg in spec["arguments"]:
        if arg["required"]:
            args.append(arg["name"])
        else:
            default_val = arg['default']
            if isinstance(default_val, str):
                args.append(f"{arg['name']}={default_val!r}")
            else:
                args.append(f"{arg['name']}={default_val!r}")
    
    signature = f"def {spec['name']}({', '.join(args)}):" if args else f"def {spec['name']}():"

    lines = [signature]
    lines.append(f"    url = {render_template_expression(spec['url'])}")

    # Query params
    if spec["query_entries"]:
        lines.append("    params = {")
        for e in spec["query_entries"]:
            lines.append(f"        {e['key']!r}: {e['expression']},")
        lines.append("    }")
    else:
        lines.append("    params = None")

    # Headers
    lines.append("    headers = {")
    for k, v in spec["headers"].items():
        lines.append(f"        {k!r}: {render_template_expression(v)},")
    lines.append("    }")

    # Payload
    if spec["payload_mode"] == "json":
        lines.append("    payload = {")
        for e in spec["payload_entries"]:
            lines.append(f"        {e['key']!r}: {e['expression']},")
        lines.append("    }")
    elif spec["payload_mode"] == "raw" and spec["payload_literal"] is not None:
        lines.append(f"    payload = {spec['payload_literal']}")
    else:
        lines.append(f"    payload = {render_template_expression(spec['data'])}")

    request_keyword = "json" if spec["payload_mode"] == "json" else "data"

    lines.append("")
    lines.append(f"    response = requests.request(method={spec['method']!r}, url=url, params=params, headers=headers, {request_keyword}=payload, impersonate='chrome')")
    lines.append(f"    response.raise_for_status()")
    lines.append(f"    print(response)")
    lines.append(f"    return {spec['name']}_parser(response)")
    return "\n".join(lines)


def render_main_function(request_specs: List[Dict[str, Any]]) -> str:
    """Generate main execution function"""
    lines = ["def do_requests():\n"]
    for idx, spec in enumerate(request_specs):
        lines.append(f"    response_{idx+1} = {spec['name']}()")
    return "\n".join(lines)


def build_request_specs_list(raw_input_list: list) -> list:
    """Build request specs with unique function names
    
    Handles both dict and Pydantic model inputs for API compatibility.
    """
    specs = []
    used_names = set()
    
    for entry in raw_input_list:
        # Handle both Pydantic models and dicts
        if hasattr(entry, 'model_dump'):
            # Pydantic v2 model - convert to dict
            entry_data = entry.model_dump()
        elif isinstance(entry, dict):
            entry_data = entry
        else:
            # Fallback: try attribute access
            entry_data = {
                'curl': getattr(entry, 'curl', ''),
                'function_name': getattr(entry, 'function_name', None)
            }
        
        curl_command = entry_data.get('curl', '')
        supplied_name = entry_data.get('function_name') or entry_data.get('name', '')
        if supplied_name:
            supplied_name = str(supplied_name).strip()
        
        spec = curl_to_requests(curl_command)

        # Generate function name
        if supplied_name:
            sanitized_fn = sanitize_identifier(supplied_name)
        else:
            method = spec.get('method', 'get').lower()
            path = urlparse(spec.get('url', '')).path.strip("/").replace("/", "_")
            sanitized_fn = sanitize_identifier(f"{method}_{path}" if path else method)

        # Ensure uniqueness
        original_fn = sanitized_fn or "request"
        suffix = 2
        while sanitized_fn in used_names or not sanitized_fn:
            sanitized_fn = f"{original_fn}_{suffix}"
            suffix += 1
        used_names.add(sanitized_fn)

        spec["name"] = sanitized_fn
        args, queries, payloads, pmode, plit = build_request_arguments(spec)
        spec.update({
            "arguments": args,
            "query_entries": queries,
            "payload_entries": payloads,
            "payload_mode": pmode,
            "payload_literal": plit,
        })
        specs.append(spec)
    return specs


def build_python_script(raw_input_list: List[Dict[str, str]]) -> Tuple[str, List[str]]:
    """Build complete Python script from curl commands"""
    specs = build_request_specs_list(raw_input_list)
    code = [
        "from curl_cffi import requests",
        "from parser import *",
        ""
    ]
    for spec in specs:
        code.append(render_request_function(spec))
        code.append("")
    code.append(render_main_function(specs))
    code.append("")
    code.append("if __name__ == '__main__':")
    code.append("    do_requests()")
    code.append("")
    return "\n".join(code), [spec['name'] for spec in specs]


def build_parser_py(function_names: List[str]) -> str:
    """Generate parser stubs"""
    code = []
    for fn in function_names:
        code.append(f"def {fn}_parser(response):")
        code.append(f"    # TODO: Implement response parsing logic")
        code.append(f"    return response.json()  # or custom parsing\n")
    return "\n".join(code)

def convert_single_curl(curl_command: str, function_name: Optional[str] = None) -> Dict[str, Any]:
    """Convert single curl command - API entry point"""
    try:
        # Pass as list of dicts (not Pydantic models) to converter
        raw_input = [{"curl": curl_command, "function_name": function_name}]
        script_code, function_names = build_python_script(raw_input)
        parser_code = build_parser_py(function_names)
        
        return {
            "success": True,
            "python_code": script_code,
            "parser_code": parser_code,
            "function_name": function_names[0] if function_names else None,
            "metadata": {
                "request_spec": build_request_specs_list(raw_input)[0]
            }
        }
    except Exception as e:
        import traceback
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }


def convert_batch_curls(commands: list) -> Dict[str, Any]:
    """Convert multiple curl commands - API entry point
    
    Accepts list of dicts or Pydantic models.
    """
    try:
        # Convert Pydantic models to dicts if needed
        raw_commands = []
        for cmd in commands:
            if hasattr(cmd, 'model_dump'):
                raw_commands.append(cmd.model_dump())
            elif isinstance(cmd, dict):
                raw_commands.append(cmd)
            else:
                raw_commands.append({'curl': str(cmd)})
        
        script_code, function_names = build_python_script(raw_commands)
        parser_code = build_parser_py(function_names)
        
        return {
            "success": True,
            "request_script": script_code,
            "parser_script": parser_code,
            "function_names": function_names,
            "metadata": {
                "total_functions": len(function_names)
            }
        }
    except Exception as e:
        import traceback
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }

# ... [keep all existing helper functions: curl_to_requests, find_placeholders, etc.] ...
def convert_curls(input_data: Union[str, Dict, List], 
                  function_name: Optional[str] = None,
                  function_name_prefix: Optional[str] = None) -> Dict[str, Any]:
    """
    Unified converter: handles single or batch curl commands
    
    Args:
        input_data: str (single curl), dict (single with options), or list (batch)
        function_name: optional name for single conversion
        function_name_prefix: optional prefix for batch auto-generated names
    
    Returns:
        Dict with conversion results
    """
    try:
        # Normalize input to list of command dicts
        if isinstance(input_data, list):
            # Batch mode
            commands = []
            for cmd in input_data:
                if isinstance(cmd, str):
                    commands.append({"curl": cmd})
                elif isinstance(cmd, dict):
                    commands.append(cmd)
                elif hasattr(cmd, 'model_dump'):
                    commands.append(cmd.model_dump(exclude_unset=True))
                else:
                    commands.append({"curl": str(cmd)})
            
            script_code, function_names = build_python_script(commands)
            parser_code = build_parser_py(function_names)
            
            return {
                "success": True,
                "request_script": script_code,
                "parser_script": parser_code,
                "function_names": function_names,
                "is_batch": True,
                "metadata": {"total_functions": len(function_names)}
            }
        
        else:
            # Single mode
            if isinstance(input_data, str):
                cmd_dict = {"curl": input_data, "function_name": function_name}
            elif isinstance(input_data, dict):
                cmd_dict = input_data.copy()
                if function_name and not cmd_dict.get('function_name'):
                    cmd_dict['function_name'] = function_name
            elif hasattr(input_data, 'model_dump'):
                cmd_dict = input_data.model_dump(exclude_unset=True)
                if function_name and not cmd_dict.get('function_name'):
                    cmd_dict['function_name'] = function_name
            else:
                cmd_dict = {"curl": str(input_data), "function_name": function_name}
            
            raw_input = [cmd_dict]
            script_code, function_names = build_python_script(raw_input)
            parser_code = build_parser_py(function_names)
            
            return {
                "success": True,
                "python_code": script_code,
                "parser_code": parser_code,
                "function_name": function_names[0] if function_names else None,
                "is_batch": False,
                "metadata": {
                    "request_spec": build_request_specs_list(raw_input)[0]
                }
            }
            
    except Exception as e:
        import traceback
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Conversion error: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }    