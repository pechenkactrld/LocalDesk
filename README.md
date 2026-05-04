# LumiDesk

> Modern desktop GUI client for Ollama with hardware-aware model recommendations, local-first workflow, and plugin-ready architecture.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![PyQt6](https://img.shields.io/badge/UI-PyQt6-green)
![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Windows-orange)
![License](https://img.shields.io/badge/License-MIT-purple)

---


```md
![Logo](screenshots/logo.png)
```


---

## Features

* Modern PyQt6 desktop UI
* Local Ollama integration
* Automatic hardware analysis
* Smart model recommendation system
* Drag & drop file upload
* Chat history saving/loading
* Fullscreen chat mode
* Model download manager (`ollama pull`)
* Linux-first development
* Planned plugin system

---

## Screenshots

### Main window

![Main window](screenshots/main.png)

### Model Download Progress

![Model Download](screenshots/download.png)

### Chat Interface

![Chat Interface](screenshots/chat.png)

---

## Installation

### Linux

```bash
sudo apt install python3 python3-pip
pip install -r requirements.txt
python main.py
```

### Windows

```bash
pip install -r requirements.txt
python main.py
```

---

## Requirements

* Python 3.11+
* Ollama installed
* Recommended: NVIDIA GPU for larger models

---

## Roadmap

* [x] Chat UI
* [x] Hardware detection
* [x] Automatic model recommendation
* [x] Model download support
* [x] Chat saving/loading
* [ ] Streaming responses
* [ ] Markdown rendering
* [ ] Plugin API
* [ ] OCR integration
* [ ] Vision model support
* [ ] RAG support
* [ ] Voice features

---

## Windows Defender Warning

> [!WARNING]
> LumiDesk is currently an unsigned open-source application.
>
> Windows Defender or SmartScreen may warn about the executable because it has no digital signature and low reputation.
>
> If you prefer:
>
> * inspect the source code manually
> * run the `.py` version directly
> * build the executable yourself

---


## Building Executable

### PyInstaller

```bash
pip install pyinstaller
pyinstaller --onefile --windowed main.py
```

---


