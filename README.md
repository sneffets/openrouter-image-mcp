# openrouter-image-mcp

Ein **MCP-Server (Python)** für **Bild- und Videogenerierung über
[OpenRouter](https://openrouter.ai)** – gebaut für den Einsatz in **Claude Code**, sowohl in
Design-Sessions als auch beim Coden (Icons, Placeholder-Assets, Mockups, OG-Images, Hero-Loops,
Produktclips …).

Modelle **und** Provider sind zur Laufzeit auflistbar und auswählbar: Der Server rät nicht,
sondern liefert eine Kandidatenliste zurück bzw. fragt per MCP-Elicitation nach, wenn kein
Modell gesetzt ist. So kann Claude dich fragen: *„Nano Banana 2 bei Google Vertex oder bei
Google AI Studio?“*

## Features

- **Modell-Auswahl** – `list_image_models` listet alle bildfähigen OpenRouter-Modelle
  (Nano Banana / Gemini Image, GPT Image, Seedream, Flux, Recraft …) inklusive Slug, Preis
  und unterstützten Parametern. Fuzzy-Suche: `query="nano banana"` findet
  `google/gemini-3.1-flash-image`.
- **Provider-Auswahl** – `list_providers` zeigt, welche Upstream-Provider ein Modell
  bedienen (z. B. `google-vertex` vs. `google-ai-studio`) samt Preis und Uptime. Die
  Auswahl wird als OpenRouter-`provider`-Routing (`order` / `only` / `allow_fallbacks` /
  `sort`) durchgereicht.
- **Generieren & Editieren** – `generate_image` und `edit_image` (Bild-zu-Bild über
  `reference_images`: lokale Pfade, http-URLs oder Data-URLs).
- **Design-Session-tauglich** – jedes Bild landet als Datei auf der Platte (Pfad wird
  zurückgegeben, damit Claude es weiterverwenden, committen oder als Referenz für die
  nächste Iteration nutzen kann) und wird zusätzlich als **verkleinerte Inline-Vorschau**
  zurückgegeben, damit der Kontext nicht zugemüllt wird.
- **Reproduzierbar** – zu jedem Bild wird eine `*.json`-Sidecar-Datei mit Prompt, Modell,
  Provider, Seed und Kosten geschrieben.
- **Robust** – Retries mit Backoff auf 429/5xx, sprechende Fehlermeldungen aus dem
  OpenRouter-Error-Body, automatischer Fallback von `POST /api/v1/images` auf
  `POST /api/v1/chat/completions` mit `modalities: ["image","text"]`.
- **Video** – `generate_video` für Text-zu-Video, Bild-zu-Video (erstes/letztes Frame),
  Referenz-zu-Video mit Bildern, Videos und Tonspuren; `edit_video` zum Editieren,
  Umstylen und Upscalen vorhandener Clips. Veo 3.1, Kling 3.0, Seedance 2.x, Sora 2, Wan,
  Hailuo, Runway, FLUX Video … – Details im Abschnitt [Video](#video).

## Installation

Voraussetzung: Python ≥ 3.10, [uv](https://docs.astral.sh/uv/) und ein OpenRouter-API-Key
(<https://openrouter.ai/settings/keys>).

Der Repo-Zugang hängt davon ab, ob das Repository öffentlich oder privat ist – das
entscheidet weiter unten auch über die möglichen `.mcp.json`-Varianten:

| Repo | Klonen | Anonymer HTTPS-Zugriff |
| --- | --- | --- |
| **öffentlich** | `git clone https://github.com/sneffets/openrouter-image-mcp.git` | funktioniert |
| **privat** | `git clone git@github.com:sneffets/openrouter-image-mcp.git` | scheitert – GitHub antwortet Unbeteiligten mit `404` |

```bash
git clone <URL aus der Tabelle>
cd openrouter-image-mcp
uv sync
```

`uv sync` legt `.venv/` an und installiert Paket samt Abhängigkeiten. Klassisch geht auch:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Smoke-Test – der Server muss auf `initialize` antworten:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  | uv run --project . openrouter-image-mcp
```

## Einrichtung in Claude Code

Drei Wege, den Server zu starten. Variante A funktioniert immer, B hängt an der
Sichtbarkeit des Repos:

| Variante | Wann sinnvoll | Repo-Sichtbarkeit |
| --- | --- | --- |
| **A** – lokaler Checkout über `uv run --project` | Entwicklung am Server selbst, verlässlichster Weg | egal |
| **B** – direkt aus GitHub über `uvx --from git+…` | ohne Checkout, z. B. für Kolleg:innen | öffentlich: `https`, privat: `ssh` + Zugriff |
| **C** – venv-Binary direkt | wenn kein uv im Spiel sein soll | egal |

### A – Lokaler Checkout (empfohlen)

```json
{
  "mcpServers": {
    "openrouter-image": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/absoluter/pfad/zu/openrouter-image-mcp",
        "openrouter-image-mcp"
      ],
      "env": {
        "OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
        "OPENROUTER_IMAGE_MODEL": "google/gemini-3.1-flash-image",
        "OPENROUTER_IMAGE_OUTPUT_DIR": "/absoluter/pfad/zum/projekt/design/images"
      }
    }
  }
}
```

### B – Direkt aus GitHub, ohne Checkout

**Öffentliches Repo:**

```json
"command": "uvx",
"args": ["--from", "git+https://github.com/sneffets/openrouter-image-mcp", "openrouter-image-mcp"]
```

**Privates Repo** – nur mit SSH-Key, der Zugriff hat:

```json
"command": "uvx",
"args": ["--from", "git+ssh://git@github.com/sneffets/openrouter-image-mcp", "openrouter-image-mcp"]
```

Die HTTPS-Schreibweise auf einem privaten Repo ist der klassische Fehlschlag: uv fetcht
ohne Anmeldung und bricht mit
`fatal: could not read Username for 'https://github.com': terminal prompts disabled` ab –
in Claude Code sichtbar nur als `CONNECTION_CLOSED`.

Beide Varianten ziehen standardmäßig den **Stand des Default-Branch**. Für reproduzierbare
Setups an einen Tag oder Commit pinnen:

```
git+https://github.com/sneffets/openrouter-image-mcp@v0.1.0
```

> **Ist eine GitHub-URL in `.mcp.json` üblich?** Verbreitet, aber nicht der Normalfall. Die
> Mehrheit der MCP-Server wird über eine Registry gestartet – `npx -y @scope/paket` bei
> Node, `uvx paketname` bei Python auf PyPI. Die `git+…`-Form ist die Notlösung für alles,
> was (noch) nicht veröffentlicht ist: sie funktioniert, kostet aber Versionierung
> (ohne `@tag` immer HEAD), braucht Git plus Netz bei jedem Kaltstart und bei privaten
> Repos zusätzlich Auth. Für einen Server, den du selbst entwickelst, ist Variante A
> deshalb meist die bessere Wahl; `git+…` lohnt sich, sobald andere ihn ohne Checkout
> benutzen sollen.

### C – venv-Binary direkt

```bash
claude mcp add openrouter-image \
  --env OPENROUTER_API_KEY=sk-or-v1-... \
  --env OPENROUTER_IMAGE_MODEL=google/gemini-3.1-flash-image \
  --env OPENROUTER_IMAGE_OUTPUT_DIR="$PWD/design/images" \
  -- /pfad/zu/openrouter-image-mcp/.venv/bin/openrouter-image-mcp
```

### Fallstricke

- **`--project`, nicht `--directory`.** `--directory` wechselt das Arbeitsverzeichnis in
  den Server-Ordner – ein relatives `OPENROUTER_IMAGE_OUTPUT_DIR=./design/images` landet
  dann im Server-Repo statt im eigenen Projekt. `--project` benutzt nur dessen venv und
  lässt das Arbeitsverzeichnis stehen.
- **Output-Verzeichnis absolut angeben**, wenn du sicher sein willst, wo die Bilder landen.
- **Key nicht im Klartext.** `${OPENROUTER_API_KEY}` wird aus der Umgebung expandiert;
  eine `.mcp.json` wird üblicherweise eingecheckt.

Prüfen mit `/mcp` in Claude Code – der Server heißt `openrouter-image`. Nach Änderungen an
`.mcp.json` muss Claude Code neu gestartet oder der Server über `/mcp` neu verbunden werden.

## Troubleshooting

**`CONNECTION_CLOSED` bzw. „Connection closed" beim Start**

Der Prozess ist gestorben, bevor der MCP-Handshake überhaupt stattfand – die Ursache liegt
also im Startbefehl, nicht im Server. Zum Nachsehen den Befehl aus `.mcp.json` von Hand
ausführen und auf stderr schauen:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  | uv run --project /pfad/zu/openrouter-image-mcp openrouter-image-mcp
```

| Meldung | Ursache |
| --- | --- |
| `could not read Username for 'https://github.com'` | `git+https://` auf ein privates Repo – auf `git+ssh://` wechseln (Variante B) oder lokal klonen (Variante A) |
| `No such file or directory: uv` / `uvx` | uv nicht im `PATH` des Clients; absoluten Pfad zur uv-Binary eintragen |
| `Failed to spawn: openrouter-image-mcp` | Paket nicht installiert – `uv sync` im Checkout nachholen |
| `ModuleNotFoundError` | Server läuft gegen ein fremdes venv; `--project` auf den Checkout zeigen lassen |

**Server startet, aber jeder Tool-Aufruf schlägt fehl**

`openrouter_status()` aufrufen – es zeigt die geladene Konfiguration und die Credits des
Keys. `OPENROUTER_API_KEY is not set` heißt, dass der `env`-Block nicht ankommt.

**Bilder landen im falschen Ordner**

Relatives `OPENROUTER_IMAGE_OUTPUT_DIR` plus `uv run --directory` – siehe oben. Wo der
Server tatsächlich hinschreibt, sagt `openrouter_status()`; einzelne Aufrufe überschreiben
das per `save_dir`.

## Konfiguration

Alles über Environment-Variablen (siehe `.env.example`):

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | – | **Pflicht.** OpenRouter-API-Key |
| `OPENROUTER_IMAGE_MODEL` | – | Default-Modell. Ohne Wert wird nach dem Modell gefragt |
| `OPENROUTER_IMAGE_PROVIDER` | – | Default-Provider, kommagetrennt (`google-vertex,google-ai-studio`) |
| `OPENROUTER_IMAGE_ALLOW_FALLBACKS` | `true` | `false` = ausschließlich die gewählten Provider |
| `OPENROUTER_IMAGE_ASK_FOR_PROVIDER` | `false` | `true` = per Elicitation nach dem Provider fragen |
| `OPENROUTER_IMAGE_OUTPUT_DIR` | `./openrouter-images` | Zielverzeichnis für generierte Bilder |
| `OPENROUTER_IMAGE_INLINE_PREVIEW` | `true` | Verkleinerte Vorschau im Tool-Ergebnis |
| `OPENROUTER_IMAGE_PREVIEW_MAX_PX` | `768` | Kantenlänge der Vorschau |
| `OPENROUTER_IMAGE_TIMEOUT` | `180` | HTTP-Timeout in Sekunden (auch für Video-Downloads) |
| `OPENROUTER_VIDEO_MODEL` | – | Default-Videomodell. Ohne Wert wird nach dem Modell gefragt |
| `OPENROUTER_VIDEO_OUTPUT_DIR` | wie Bilder | Zielverzeichnis für Videos |
| `OPENROUTER_VIDEO_POLL_INTERVAL` | `15` | Sekunden zwischen zwei Status-Abfragen eines Video-Jobs |
| `OPENROUTER_VIDEO_MAX_WAIT` | `600` | So lange wartet ein Tool-Aufruf maximal auf ein Video |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | API-Basis-URL |
| `OPENROUTER_APP_TITLE` / `OPENROUTER_APP_URL` | Projektname/-URL | Attribution-Header für OpenRouter |

## Tools

| Tool | Zweck |
| --- | --- |
| `list_image_models(query, limit, refresh)` | Bildfähige Modelle auflisten/filtern |
| `describe_image_model(model)` | Details: Preis, Parameter, max. Referenzbilder, Provider |
| `list_providers(model)` | Provider für ein Modell – oder alle Provider |
| `generate_image(prompt, model, providers, …)` | Bild erzeugen, speichern, Vorschau liefern |
| `edit_image(prompt, reference_images, …)` | Bestehende Bilder editieren/variieren |
| `show_image(path, max_pixels)` | Beliebiges lokales Bild inline anzeigen |
| `list_generated_images(limit)` | Zuletzt erzeugte Bilder im Output-Verzeichnis |
| `list_video_models(query, limit, refresh)` | Videomodelle mit Preis, Längen, Auflösungen, Frames, Audio |
| `describe_video_model(model)` | Alles, was ein Videomodell kann, inkl. `provider_options` |
| `generate_video(prompt, model, …)` | Video erzeugen, warten, speichern, Frame-Vorschau liefern |
| `edit_video(source_video, prompt, …)` | Clip editieren, umstylen oder upscalen |
| `check_video_job(job_id)` | Laufenden Job weiter abwarten und herunterladen |
| `list_generated_videos(limit)` | Offene Jobs + zuletzt gespeicherte Videos |
| `show_video(path)` | Lokales Video inspizieren (Länge, Auflösung, Audio, 4-Frame-Vorschau) |
| `openrouter_status()` | Konfiguration + Credit-/Limit-Infos des Keys |

Zusätzlich als Ressourcen: `openrouter://image-models` und `openrouter://video-models`.

`generate_image` reicht die normalisierten OpenRouter-Parameter durch: `aspect_ratio`,
`resolution`, `quality`, `output_format`, `background`, `seed`, `n` sowie `extra_body` als
Escape-Hatch für alles Weitere. Welche ein Modell wirklich unterstützt, sagt
`describe_image_model`.

## Beispiele

**Design-Session – erst auswählen, dann generieren**

> „Welche Image-Modelle gibt es bei Google?“
> → `list_image_models(query="google")`
>
> „Wer serviert Nano Banana 2?“
> → `list_providers(model="nano banana 2")` → `google-vertex`, `google-ai-studio`
>
> „Nimm Nano Banana 2 über Vertex, 16:9, Hero-Image für die Landing-Page.“
> → `generate_image(prompt="…", model="google/gemini-3.1-flash-image",
>    providers=["google-vertex"], aspect_ratio="16:9")`

**Iterieren auf einem Ergebnis**

> `edit_image(prompt="gleiche Szene, aber Nachtstimmung",
>  reference_images=["design/images/20260902-120000-hero.png"])`

**Für Code**

> „Generier mir ein 512×512-App-Icon als PNG mit transparentem Hintergrund und leg es
> nach `assets/`.“
> → `generate_image(..., background="transparent", save_dir="assets")`

## Video

Videos laufen bei OpenRouter als **asynchrone Jobs** (`POST /api/v1/videos` → Status pollen →
Download). Der Server nimmt dir das ab: `generate_video` schickt den Job ab, meldet
Fortschritt per MCP-Progress, lädt das fertige Video herunter und legt es samt
`*.json`-Sidecar ab.

### Was geht

| Modus | Parameter | Hinweis |
| --- | --- | --- |
| Text-zu-Video | `prompt` | alle Modelle |
| Bild-zu-Video | `first_frame`, `last_frame` | Start- und/oder Endbild; welche Frames gehen, zeigt `describe_video_model` |
| Referenzbilder | `reference_images` | Figuren, Produkte, Stil – lose Vorgabe statt exaktem Frame |
| Referenzvideos | `reference_videos` | Bewegung, Kamera, Effekt-Vorlage, Clip fortsetzen |
| Tonspuren | `reference_audios` | Musik, Sound, Stimme, auf die das Video abgestimmt wird |
| Ton erzeugen | `generate_audio` | Veo, Kling, Seedance, Wan, Sora … erzeugen passenden Ton |
| Editieren/Upscalen | `edit_video(source_video, …)`, `upscale_factor`, `creativity` | z. B. FLUX Video Edit, Runway Aleph, FLUX Video Upscale |
| Format | `duration`, `resolution`, `aspect_ratio`, `size`, `seed` | werden vor dem Absenden gegen das Modell geprüft |
| Modellspezifisch | `provider_options`, `negative_prompt` | z. B. `personGeneration` bei Veo, `cfg_scale` bei Kling |

Alle Eingaben akzeptieren lokale Pfade, http-URLs oder Data-URLs. Lokale Dateien werden
inline mitgeschickt (Video bis 50 MB, Audio bis 20 MB) – für große Clips ist eine öffentliche
URL robuster. **Video- und Audio-Referenzen werden nur von Modellen berücksichtigt, die das
können** (laut OpenRouter u. a. Seedance ab Generation 2); andere ignorieren sie stillschweigend.

`negative_prompt` wird automatisch auf die Schreibweise des Modells gemappt (`negativePrompt`
bei Veo, `negative_prompt` bei Kling/Wan) und wie alle `provider_options` unter
`provider.options.<provider>.parameters` einsortiert – den Provider sucht der Server selbst.
Unbekannte Optionen, falsche Längen oder nicht unterstützte Frames werden **vor** dem
kostenpflichtigen Request abgelehnt, mit der Liste der erlaubten Werte. Wer es trotzdem
schicken will: `extra_body`.

### Aktuelle Top-Modelle (Stand 2026-09-13)

| | Veo 3.1 | Kling 3.0 Pro | Seedance 2.5 |
| --- | --- | --- | --- |
| Slug | `google/veo-3.1` | `kwaivgi/kling-v3.0-pro` | `bytedance/seedance-2.5` |
| Länge | 4, 6, 8 s | 3–15 s | 4–30 s |
| Auflösung | 720p, 1080p, 4K | 720p | 480p, 720p |
| Seitenverhältnis | 16:9, 9:16 | 16:9, 9:16, 1:1 | 16:9, 4:3, 1:1, 3:4, 9:16, 21:9 |
| Erstes/letztes Frame | ✓ / ✓ | ✓ / ✓ | ✓ / ✓ |
| Ton erzeugen | ✓ | ✓ | ✓ |
| Seed | ✓ | – | ✓ |
| Video-/Audio-Referenzen | – | – | ✓ |
| `provider_options` | `personGeneration`, `negativePrompt`, `enhancePrompt`, `conditioningScale`, `aspectRatio` | `negative_prompt`, `cfg_scale` | `watermark`, `req_key`, `output_format` |
| Preis | $0.20/s ohne, $0.40/s mit Ton (4K: $0.40/$0.60) | $0.112/s, mit Ton $0.168/s | $10.70 / M Video-Tokens |

Günstigere Varianten: `google/veo-3.1-fast`, `google/veo-3.1-lite`, `kwaivgi/kling-v3.0-std`,
`bytedance/seedance-2.0-fast`/`-mini`. Die Tabelle ist eine Momentaufnahme –
`list_video_models` und `describe_video_model` zeigen immer den Live-Stand.

### Warten, Timeouts, Kosten

- Ein Video braucht **30 Sekunden bis mehrere Minuten**. `generate_video` wartet bis zu
  `OPENROUTER_VIDEO_MAX_WAIT` (Default 600 s, pro Aufruf über `max_wait_seconds`).
- Ist der Job dann noch nicht fertig, kommt statt eines Fehlers die **Job-ID** zurück. Der Job
  läuft bei OpenRouter weiter; `check_video_job(job_id=…)` wartet weiter und lädt herunter.
  **Nicht neu absenden** – das kostet doppelt. Offene Jobs listet `list_generated_videos`.
- Mit `wait=false` wird nur abgeschickt – praktisch, um mehrere Varianten parallel zu starten.
- Jobs werden unter `<Video-Verzeichnis>/.video-jobs/` gemerkt und überleben so auch einen
  Neustart des Servers.
- Der Job-Start wird bewusst **nicht automatisch wiederholt** (außer bei 429), damit ein
  Timeout nicht zu einem zweiten bezahlten Video führt.
- Ist **ffmpeg** installiert, gibt es als Vorschau ein 2×2-Raster aus vier gleichmäßig
  verteilten Frames plus Länge/Auflösung/Tonspur-Info. Ohne ffmpeg funktioniert alles andere
  genauso, nur ohne Vorschau.

### Beispiele

**Text-zu-Video mit Ton**

> `generate_video(prompt="Langsame Kamerafahrt über eine neblige Berglandschaft bei
>  Sonnenaufgang, Vogelgezwitscher", model="google/veo-3.1", duration=8,
>  resolution="1080p", aspect_ratio="16:9", generate_audio=true,
>  negative_prompt="Text, Wasserzeichen")`

**Hero-Image animieren (Start- und Endbild)**

> `generate_video(prompt="Das Produkt dreht sich einmal langsam um die eigene Achse",
>  model="kwaivgi/kling-v3.0-pro", first_frame="design/images/hero.png",
>  last_frame="design/images/hero-back.png", duration=5,
>  provider_options={"cfg_scale": 0.6})`

**Referenzen kombinieren: Figur + Bewegungsvorlage + Musik**

> `generate_video(prompt="Die Figur aus dem Bild tanzt wie im Referenzclip, im Takt der Musik",
>  model="bytedance/seedance-2.5", reference_images=["assets/figur.png"],
>  reference_videos=["https://example.com/tanz.mp4"], reference_audios=["assets/beat.mp3"],
>  duration=10, aspect_ratio="9:16")`

**Clip editieren und hochskalieren**

> `edit_video(source_video="clips/demo.mp4", prompt="gleiche Szene, aber bei Nacht im Regen",
>  model="black-forest-labs/flux-video-edit")`
>
> `edit_video(source_video="clips/demo-night.mp4", model="black-forest-labs/flux-video-upscale",
>  upscale_factor=2, creativity=0)`

## Modell- und Provider-Auswahl

1. Ist `model` gesetzt, wird es benutzt – Slug oder Klarname (`"nano banana 2"`) sind ok.
2. Sonst greift `OPENROUTER_IMAGE_MODEL`.
3. Sonst versucht der Server eine **MCP-Elicitation** (Client fragt den User).
4. Unterstützt der Client keine Elicitation, kommt statt eines Fehlers eine
   **Kandidatenliste** zurück – Claude legt sie dir vor und fragt nach.

Beim Provider ist die Route standardmäßig automatisch (OpenRouter wählt). Mit
`providers=[...]` pinnst du sie, mit `allow_fallbacks=false` hart, und mit
`OPENROUTER_IMAGE_ASK_FOR_PROVIDER=true` wird bei mehreren Providern aktiv nachgefragt.

## Entwicklung

```bash
uv pip install -e ".[dev]"
.venv/bin/pytest        # Tests laufen komplett offline gegen einen Mock-Transport
.venv/bin/ruff check .
```

Aufbau:

| Modul | Inhalt |
| --- | --- |
| `config.py` | Environment-Settings |
| `client.py` | HTTP-Client, Retries, Endpoint-Fallback |
| `results.py` | Normalisierung der Response-Formen |
| `imaging.py` | Speichern, Sidecars, Vorschauen, Referenzbilder |
| `catalog.py` | Modell-/Provider-Discovery samt Cache und Formatierung |
| `generation.py` | Request-Bau und Persistenz |
| `media.py` | Video-/Audio-Inputs, ffprobe-Infos, Frame-Vorschau per ffmpeg |
| `video.py` | Video-Requests, Validierung gegen Modellfähigkeiten, Polling, Job-Records, Download |
| `server.py` | MCP-Tools |

## Lizenz

MIT – siehe [LICENSE](LICENSE).
