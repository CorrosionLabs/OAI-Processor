from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from core.ids import build_conversation_filename, slugify


_CSS = """
:root {
  color-scheme: dark; font-family: system-ui, sans-serif;
  --page-bg: #111111; --sidebar-bg: #222222; --text: #dddddd;
  --link: #ffffff; --border: #444444; --input-border: #666666;
  --secondary: #aaaaaa; --missing: #ffffff;
}
:root[data-theme="light"] {
  color-scheme: light;
  --page-bg: #ffffff; --sidebar-bg: #f0f0f0; --text: #333333;
  --link: #000000; --border: #cccccc; --input-border: #999999;
  --secondary: #666666; --missing: #000000;
}
* { box-sizing: border-box; }
body { margin: 0; color: var(--text); background: var(--page-bg); }
a { color: var(--link); }
.layout { display: grid; grid-template-columns: 17rem minmax(0, 1fr); min-height: 100vh; }
.sidebar { position: sticky; top: 0; height: 100vh; overflow-y: auto; padding: 1.25rem; background: var(--sidebar-bg); border-right: 1px solid var(--border); }
.sidebar h2 { margin: 0 0 1rem; font-size: 1rem; }
.sidebar-options { margin-bottom: .75rem; padding: .75rem; background: #303030; border: 1px solid var(--border); border-radius: .4rem; }
:root[data-theme="light"] .sidebar-options { background: #e2e2e2; }
.sidebar-options summary { cursor: pointer; }
.sidebar-options-content { display: grid; gap: .75rem; margin-top: .75rem; }
.sidebar-controls { display: flex; flex-wrap: wrap; gap: .4rem; }
.sidebar input, .sidebar select, .sidebar button { color: inherit; background: var(--page-bg); border: 1px solid var(--input-border); border-radius: .35rem; font: inherit; }
.sidebar input { width: 100%; padding: .65rem .75rem; }
.sidebar select, .sidebar button { padding: .4rem .55rem; }
.sidebar button { cursor: pointer; }
.sidebar button:hover { background: var(--border); }
.sidebar nav { margin-top: 1rem; }
.nav-project { margin: .35rem 0; }
.nav-project summary { padding: .35rem 0; cursor: pointer; overflow-wrap: anywhere; }
.nav-project ul { margin: .25rem 0 .75rem; padding-left: 1.25rem; list-style: none; }
.nav-project li { margin: .3rem 0; overflow-wrap: anywhere; }
.nav-project a { display: block; padding: .3rem .4rem; text-decoration: none; border-radius: .25rem; }
.nav-project a:hover, .nav-project a[aria-current="page"] { background: var(--border); }
main { min-width: 0; width: 100%; max-width: 64rem; padding: 2rem clamp(1rem, 4vw, 3rem); }
h1 { margin: 0 0 1.25rem; line-height: 1.25; overflow-wrap: anywhere; }
.metadata { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: .65rem 1.5rem; margin: 0 0 2rem; color: var(--secondary); font-size: .9rem; }
.metadata div { min-width: 0; }
.metadata dt { font-weight: 600; }
.metadata dd { margin: .2rem 0 0; overflow-wrap: anywhere; }
.message { padding: 1.35rem 0; border-top: 1px solid var(--border); }
.message header { display: flex; flex-wrap: wrap; align-items: baseline; gap: .5rem 1rem; margin-bottom: .7rem; }
.message h2 { margin: 0; font-size: 1.05rem; }
.message time { color: var(--secondary); font-size: .85rem; overflow-wrap: anywhere; }
.message-text { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.6; }
.resources { display: grid; gap: .75rem; margin-top: 1rem; }
.resource { margin: 0; min-width: 0; }
.resource img, .resource video { display: block; max-width: 100%; max-height: 35rem; border-radius: .3rem; }
.resource audio { display: block; width: min(100%, 32rem); }
.resource figcaption { margin-top: .35rem; color: var(--secondary); font-size: .85rem; overflow-wrap: anywhere; }
.missing { color: var(--missing); overflow-wrap: anywhere; }
@media (max-width: 760px) {
  .layout { grid-template-columns: minmax(0, 1fr); }
  .sidebar { position: static; height: auto; max-height: 35vh; border-right: 0; border-bottom: 1px solid var(--border); }
  main { padding: 1.5rem 1rem; }
}
""".strip()


_SEARCH_SCRIPT = """
(() => {
  const search = document.querySelector('#navigation-search');
  const projects = Array.from(document.querySelectorAll('.nav-project'));
  const theme = document.querySelector('#navigation-theme');
  const projectStateKey = 'oai-export-project-state';
  let projectState = {};
  try {
    const storedProjectState = JSON.parse(sessionStorage.getItem(projectStateKey) || '{}');
    if (storedProjectState && typeof storedProjectState === 'object' && !Array.isArray(storedProjectState)) {
      projectState = storedProjectState;
    }
  } catch {
    projectState = {};
  }
  const saveProjectState = () => {
    try {
      sessionStorage.setItem(projectStateKey, JSON.stringify(projectState));
    } catch {
      // sessionStorage may be unavailable in restricted browser contexts.
    }
  };
  document.documentElement.dataset.theme = theme.value;
  theme.addEventListener('change', () => {
    document.documentElement.dataset.theme = theme.value;
  });
  for (const project of projects) {
    const projectName = project.dataset.name;
    if (typeof projectState[projectName] === 'boolean') {
      project.open = projectState[projectName];
    }
    project.dataset.preferredOpen = String(project.open);
    project.querySelector('summary').addEventListener('click', () => {
      project.dataset.preferredOpen = String(!project.open);
      projectState[projectName] = !project.open;
      saveProjectState();
    });
  }
  document.querySelector('#navigation-open-all').addEventListener('click', () => {
    for (const project of projects) {
      if (project.hidden) continue;
      project.dataset.preferredOpen = 'true';
      project.open = true;
      projectState[project.dataset.name] = true;
    }
    saveProjectState();
  });
  document.querySelector('#navigation-close-all').addEventListener('click', () => {
    for (const project of projects) {
      project.dataset.preferredOpen = 'false';
      project.open = false;
      projectState[project.dataset.name] = false;
    }
    saveProjectState();
  });
  search.addEventListener('input', () => {
    const query = search.value.trim().toLocaleLowerCase();
    for (const project of projects) {
      const projectMatches = project.dataset.name.toLocaleLowerCase().includes(query);
      let hasResults = false;
      for (const item of project.querySelectorAll('li')) {
        const matches = !query || projectMatches || item.dataset.title.toLocaleLowerCase().includes(query);
        item.hidden = !matches;
        hasResults ||= matches;
      }
      project.hidden = !!query && !hasResults && !projectMatches;
      project.open = query ? !project.hidden : project.dataset.preferredOpen === 'true';
    }
  });
})();
""".strip()


def export_conversation(
    conversation: dict[str, Any],
    project_slug: str,
    output_root: Path,
    resource_map: dict[str, str],
    navigation: list[dict[str, Any]],
    custom_roles: bool = False,
    user_role: str = "Usuario",
    assistant_role: str = "Asistente",
) -> tuple[Path, set[str], list[dict[str, Any]]]:
    """Write one conversation and return its path, used resources, and missing resources.

    navigation contains projects with a ``name`` and ``conversations`` list. Each
    conversation has a ``title`` and a ``path`` relative to this HTML file.
    """
    conversation_id = str(conversation.get("conversation_id") or "conversation")
    title = str(conversation.get("title") or "Sin título").strip() or "Sin título"
    project_dir = output_root / slugify(project_slug or "unassigned", limit=64)
    project_dir.mkdir(parents=True, exist_ok=True)
    clean_filename = Path(build_conversation_filename(conversation_id, title)).with_suffix(".html").name
    output_path = _conversation_output_path(project_dir, clean_filename, conversation_id)
    document, references, missing_resources = _render_html(
        conversation,
        project_slug,
        title,
        conversation_id,
        output_path,
        resource_map,
        navigation,
        custom_roles=custom_roles,
        user_role=user_role,
        assistant_role=assistant_role,
    )
    output_path.write_text(document, encoding="utf-8")
    return output_path, references, missing_resources


def export_index(output_root: Path, navigation: list[dict[str, Any]]) -> Path:
    """Write the HTML landing page with conversation paths relative to output_root."""
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "index.html"
    parts = _page_start("OpenAI Export", navigation, output_path)
    parts.extend(["<h1>OpenAI Export</h1>", "<p>Selecciona una conversación en el menú.</p>"])
    parts.extend(_page_end())
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return output_path


def _conversation_output_path(project_dir: Path, clean_filename: str, conversation_id: str) -> Path:
    existing_by_name: dict[str, list[Path]] = {}
    for path in project_dir.iterdir():
        existing_by_name.setdefault(path.name.casefold(), []).append(path)

    clean_path = Path(clean_filename)
    stem = clean_path.stem
    suffix = clean_path.suffix
    number = 1
    while True:
        filename = clean_filename if number == 1 else f"{stem}__{number}{suffix}"
        matches = existing_by_name.get(filename.casefold(), [])
        if not matches:
            return project_dir / filename
        for existing in matches:
            if existing.is_file() and not existing.is_symlink() and _html_conversation_id(existing) == conversation_id:
                return existing
        number = 2 if number == 1 else number + 1


class _ConversationIdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.conversation_id: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "html":
            self.conversation_id = dict(attrs).get("data-conversation-id")


def _html_conversation_id(path: Path) -> str | None:
    try:
        parser = _ConversationIdParser()
        parser.feed(path.read_text(encoding="utf-8"))
        return parser.conversation_id
    except (OSError, UnicodeError):
        return None


def _render_html(
    conversation: dict[str, Any],
    project_slug: str,
    title: str,
    conversation_id: str,
    output_path: Path,
    resource_map: dict[str, str],
    navigation: list[dict[str, Any]],
    *,
    custom_roles: bool,
    user_role: str,
    assistant_role: str,
) -> tuple[str, set[str], list[dict[str, Any]]]:
    references: set[str] = set()
    missing_resources: list[dict[str, Any]] = []
    role_names = {
        "user": user_role if custom_roles else "Usuario",
        "assistant": assistant_role if custom_roles else "Asistente",
        "system": "Sistema",
        "tool": "Herramienta",
    }
    parts = _page_start(title, navigation, output_path, conversation_id)
    parts.extend([
        f"<h1>{escape(title)}</h1>",
        _render_metadata(conversation, project_slug),
    ])
    messages = conversation.get("messages")
    if not isinstance(messages, list):
        messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown").lower()
        role_name = role_names.get(role, role.capitalize())
        parts.extend(['<article class="message">', "<header>", f"<h2>{escape(role_name)}</h2>"])
        created_at = message.get("created_at")
        if isinstance(created_at, str) and created_at.strip():
            parts.append(f"<time>{escape(created_at.strip())}</time>")
        parts.append("</header>")
        message_text = _message_text(message)
        if message_text:
            parts.append(f'<div class="message-text">{escape(message_text)}</div>')
        resources = message.get("resources")
        if isinstance(resources, list):
            resource_parts: list[str] = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                source_key = _resource_key(resource)
                final_path = resource_map.get(source_key)
                if not isinstance(final_path, str) or not _safe_resource_path(final_path):
                    missing_resources.append({**resource, "_export_source_key": source_key})
                    name = _resource_name(resource, source_key)
                    resource_parts.append(f'<p class="resource missing">Recurso no disponible: {escape(name)}</p>')
                    continue
                references.add(final_path)
                name = _resource_name(resource, final_path)
                url = escape(quote(final_path, safe="/"), quote=True)
                kind = str(resource.get("kind") or "").lower()
                mime = str(resource.get("mime_type") or "").lower()
                if kind == "image" or mime.startswith("image/"):
                    content = f'<img src="{url}" alt="{escape(name, quote=True)}" loading="lazy">'
                elif kind == "audio" or mime.startswith("audio/"):
                    content = f'<audio controls preload="none" src="{url}">{escape(name)}</audio>'
                elif kind == "video" or mime.startswith("video/"):
                    content = f'<video controls preload="none" src="{url}">{escape(name)}</video>'
                else:
                    content = f'<a href="{url}">{escape(name)}</a>'
                resource_parts.append(f'<figure class="resource">{content}<figcaption>{escape(name)}</figcaption></figure>')
            if resource_parts:
                parts.extend(['<div class="resources">', *resource_parts, "</div>"])
        parts.append("</article>")
    parts.extend(_page_end())
    return "\n".join(parts) + "\n", references, missing_resources


def _page_start(
    title: str,
    navigation: list[dict[str, Any]],
    output_path: Path,
    conversation_id: str | None = None,
) -> list[str]:
    html_tag = (
        '<html lang="es">'
        if conversation_id is None
        else f'<html lang="es" data-conversation-id="{escape(conversation_id, quote=True)}">'
    )
    return [
        "<!doctype html>",
        html_tag,
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        '<div class="layout">',
        _render_navigation(navigation, output_path),
        "<main>",
    ]


def _page_end() -> list[str]:
    return ["</main>", "</div>", f"<script>{_SEARCH_SCRIPT}</script>", "</body>", "</html>"]


def _render_metadata(conversation: dict[str, Any], project_slug: str) -> str:
    fields = (
        ("Proyecto", project_slug or "unassigned"),
        ("ID", conversation.get("conversation_id")),
        ("ID de OpenAI", conversation.get("openai_conversation_id")),
        ("Creada", conversation.get("created_at")),
        ("Actualizada", conversation.get("updated_at")),
        ("Modelo", conversation.get("model")),
    )
    entries = [
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(value))}</dd></div>"
        for label, value in fields if value is not None and str(value).strip()
    ]
    return '<dl class="metadata">' + "".join(entries) + "</dl>" if entries else ""


def _render_navigation(navigation: list[dict[str, Any]], output_path: Path) -> str:
    parts = [
        '<aside class="sidebar">',
        "<h2>Conversaciones</h2>",
        '<details class="sidebar-options">',
        '<summary>Opciones</summary>',
        '<div class="sidebar-options-content">',
        '<label for="navigation-theme">Tema</label>',
        '<select id="navigation-theme" aria-label="Tema">',
        '<option value="dark" selected>Oscuro</option>',
        '<option value="light">Claro</option>',
        '</select>',
        '<div class="sidebar-controls">',
        '<button id="navigation-open-all" type="button">Abrir todo</button>',
        '<button id="navigation-close-all" type="button">Colapsar todo</button>',
        '</div>',
        '<input id="navigation-search" type="search" placeholder="Buscar…" aria-label="Buscar proyectos y conversaciones">',
        '</div>',
        '</details>',
        '<nav aria-label="Proyectos y conversaciones">',
    ]
    for project in navigation:
        if not isinstance(project, dict):
            continue
        project_name = str(project.get("name") or project.get("project_name") or project.get("slug") or "Proyecto")
        parts.append(f'<details class="nav-project" data-name="{escape(project_name, quote=True)}">')
        parts.append(f"<summary>{escape(project_name)}</summary>")
        parts.append("<ul>")
        conversations = project.get("conversations")
        if isinstance(conversations, list):
            for entry in conversations:
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title") or entry.get("name") or "Sin título")
                relative_path = entry.get("path") or entry.get("relative_path")
                if not isinstance(relative_path, str) or not _safe_navigation_path(relative_path):
                    continue
                url = escape(quote(relative_path, safe="/"), quote=True)
                current = ' aria-current="page"' if relative_path == output_path.name else ""
                parts.append(
                    f'<li data-title="{escape(title, quote=True)}"><a href="{url}"{current}>{escape(title)}</a></li>'
                )
        parts.extend(["</ul>", "</details>"])
    parts.extend(["</nav>", "</aside>"])
    return "\n".join(parts)


def _safe_navigation_path(path: str) -> bool:
    if not path or "\\" in path or path.startswith(("/", "#")) or ":" in path or "?" in path or "#" in path:
        return False
    return PurePosixPath(path).suffix.lower() == ".html"


def _safe_resource_path(path: str) -> bool:
    if not path or "\\" in path or ":" in path or "?" in path or "#" in path:
        return False
    relative = PurePosixPath(path)
    return not relative.is_absolute() and len(relative.parts) > 1 and relative.parts[0] == "resources" and ".." not in relative.parts


def _message_text(message: dict[str, Any]) -> str:
    text = message.get("text")
    message_text = text.strip() if isinstance(text, str) else ""
    if message_text:
        return message_text
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    return "\n\n".join(
        part["text"].strip()
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip()
    )


def _resource_key(resource: dict[str, Any]) -> str:
    relative = resource.get("relative_resource_path")
    if isinstance(relative, str) and relative.strip():
        return Path(relative).as_posix()
    raw = resource.get("raw_relative_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).as_posix().removeprefix("resources/")
    return ""


def _resource_name(resource: dict[str, Any], fallback: str) -> str:
    return str(
        resource.get("original_name")
        or resource.get("resource_id")
        or resource.get("openai_asset_id")
        or resource.get("openai_file_id")
        or Path(fallback).name
        or "recurso"
    )
