# LocalDesk

> Modern desktop GUI client for Ollama with hardware-aware model recommendations, local-first workflow, and plugin-ready architecture.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![PyQt6](https://img.shields.io/badge/UI-PyQt6-green)
![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20Windows%20%7C%20macOS-orange)
![License](https://img.shields.io/badge/License-MIT-purple)

---

## Screenshots

### Main window

![Main window](screenshots/main.png)

### Chat Interface

![Chat Interface](screenshots/chat.png)

---

## Installation

### Windows

Download Ollama from their official web site and install it, 
Download LocalDesk.exe from GitHub releases

### MacOS

Download Ollama from their official web site and install it, 
Download LocalDesk.dmg file from GitHub releases
Drag it to applications folder

### Linux

Download Ollama from their official web site and install it, 
Download LocalDesk.bin from GitHub releases or using git clone

then

```bash
cd ~/Folder-with-LocalDesk
chmod +x LocalDesk.bin
./LocalDesk.bin
```

---

## Requirements

* Python 3.11+
* Ollama installed and in PATH
* Recommended: NVIDIA GPU for larger models

---

## Roadmap

* [x] Chat UI
* [x] Hardware detection
* [x] Automatic model recommendation
* [x] Model download support
* [x] Chat saving/loading
* [x] Streaming responses
* [x] Settings tab
* [x] Temperature controls
* [ ] System tray / quick assistant mode
* [ ] Markdown rendering
* [ ] Plugin API
* [ ] RAG support
* [ ] Voice features

---

## Windows Defender Warning

> [!WARNING]
> Windows Defender or SmartScreen may warn about the executable because it has no digital signature and low reputation.
>
> If you prefer:
>
> * inspect the source code manually
> * run the `.py` version directly
> * build the executable yourself

---

### Bug reports
This is the first release — if you find any bugs, please open an issue on GitHub.
I'll do my best to fix them quickly!

---


