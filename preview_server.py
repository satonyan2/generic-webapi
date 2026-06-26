import json
import os
import pathlib
import re
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler
from socketserver import TCPServer


ROOT = pathlib.Path(__file__).resolve().parent
PUBLIC_ROOT = ROOT / "public"
PORT = int(os.environ.get("PORT", "8080"))
PROVIDER = os.environ.get("PROVIDER", "openai")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
OPENAI_API_ENDPOINT = "https://api.openai.com/v1/chat/completions"
MAX_COUNT = 20
MAX_DAYS = 5


def load_env(path):
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def fill_template(template, variables):
    def replace(match):
        key = match.group(1)
        return str(variables[key]) if key in variables else match.group(0)

    return re.sub(r"\$\{(\w+)\}", replace, template)


def extract_array(response_text):
    parsed = json.loads(response_text)
    for value in parsed.values():
        if isinstance(value, list):
            return value
    raise ValueError("No array found in the LLM response object.")


def call_openai(prompt):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable is not set")

    body = json.dumps(
        {
            "model": MODEL,
            "messages": [{"role": "system", "content": prompt}],
            "max_completion_tokens": 2000,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        OPENAI_API_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error", {}).get("message", detail)
        except json.JSONDecodeError:
            message = detail
        raise ValueError(message) from error

    return data["choices"][0]["message"]["content"]


class PreviewHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        url_path = urllib.parse.urlparse(path).path
        rel_path = url_path.lstrip("/") or "index.html"
        return str((PUBLIC_ROOT / rel_path).resolve())

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/api/pages":
            files = sorted(
                path.name
                for path in PUBLIC_ROOT.glob("*.html")
                if path.name != "index.html"
            )
            json_response(self, 200, files)
            return

        super().do_GET()

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            title = payload.pop("title", "Generated Content")
            app_name = payload.pop("app", "quiz")
            return_raw = payload.pop("returnRaw", False)

            if app_name not in PROMPT_TEMPLATES:
                json_response(self, 400, {"error": "Invalid app type"})
                return

            if "count" in payload:
                count = payload["count"]
                if not isinstance(count, int) or count < 1 or count > MAX_COUNT:
                    json_response(
                        self,
                        400,
                        {"error": f"count must be an integer between 1 and {MAX_COUNT}"},
                    )
                    return

            if "days" in payload:
                days = payload["days"]
                if not isinstance(days, int) or days < 1 or days > MAX_DAYS:
                    json_response(
                        self,
                        400,
                        {"error": f"days must be an integer between 1 and {MAX_DAYS}"},
                    )
                    return

            if PROVIDER != "openai":
                json_response(self, 400, {"error": "Preview server supports openai only"})
                return

            prompt = fill_template(PROMPT_TEMPLATES[app_name], payload)
            response_text = call_openai(prompt)
            data = extract_array(response_text)
            json_response(
                self,
                200,
                {
                    "title": title,
                    "data": data,
                    "rawJson": response_text if return_raw else None,
                },
            )
        except Exception as error:
            print("Preview API Error:", error)
            json_response(
                self,
                500,
                {"error": "Failed to generate content. Please try again."},
            )


load_env(ROOT / ".env.local")
PROMPT_TEMPLATES = {
    "quiz": (ROOT / "prompt.md").read_text(encoding="utf-8"),
    "trip": (ROOT / "prompt-trip.md").read_text(encoding="utf-8"),
}


if __name__ == "__main__":
    with TCPServer(("", PORT), PreviewHandler) as httpd:
        print(f"Preview server running on http://localhost:{PORT}")
        print(f"Config: {PROVIDER} - {MODEL}")
        httpd.serve_forever()
