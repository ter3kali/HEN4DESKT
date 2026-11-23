# HEN4DESKT — Doujin Reader

A desktop image reader for manga, webtoons, and other image-based books. It provides continuous vertical scrolling, a local library and reading history, and an optional `gallery-dl`-powered search and download interface.

> **Content notice:** Discover and Download can access sources that host adult material. Use the available site/rating filters, comply with the laws and terms that apply to you, and only download content you are authorized to access.

## Features

- **Continuous reader:** vertically scroll through image folders, with smooth scrolling, auto-scroll, adjustable image width and page spacing.
- **Responsive rendering:** page decoding and resizing run in background workers; tall images are split into tiles to keep memory use manageable.
- **Home screen:** browse reading history, scan a local library, continue reading, bookmark books, and view suggestions.
- **Library tools:** search and filter books, group chapters, track progress, preview covers, and open folders in your file manager.
- **Discover & Download (optional):** search supported sources, browse tag results, submit codes or URLs, inspect a download queue, and save searches. Cover thumbnails and site availability depend on the source and your `gallery-dl` setup.
- **Reader customization:** multiple themes, high-DPI scaling on Windows, fullscreen and zen modes.

## Requirements

- Python 3.8 or newer is recommended.
- Pillow is required.
- Tkinter is required (it is included with many Python installations; some Linux distributions package it separately).
- `gallery-dl` is optional and enables Discover & Download. `requests` is used with the downloader for cover thumbnails and is commonly installed as a dependency of `gallery-dl`.

## Install and run

Clone or download this repository, then from its directory run:

```bash
python -m pip install Pillow
python reader.py
```

To enable Discover & Download:

```bash
python -m pip install -U gallery-dl
python reader.py
```

Open a folder or select image files from the app. You can also pass a folder or image path when launching:

```bash
python reader.py "/path/to/book"
```

On Windows, use `py` in place of `python` if that is how Python is installed. On Linux, install your distribution's Tkinter package if importing `tkinter` fails (for example, `python3-tk` on Debian-based systems).

## Using the app

- **Open a book:** use **Folder** or **Files**, or press `O` / `F`.
- **Library and history:** use the Home screen or press `L` while reading to toggle the side panel. Choose a library folder from the Home screen to scan books.
- **Discover & Download:** open **Get** or press `D`. Set a download folder in the settings when prompted. The download folder is also used as the library root.
- **Resume:** press `C` to open the most recently available book.
- **Chapter navigation:** `N` and `P` open the next or previous sibling chapter folder.

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| `Up` / `Down`, `J` / `K` | Scroll |
| `Space` / `Shift` + `Space` | Scroll down / up by a page |
| `Page Up` / `Page Down` | Scroll up / down |
| `Home` / `End` | Go to top / bottom |
| `+` / `-` | Increase / decrease image width |
| `Ctrl` + mouse wheel | Change image width |
| `T` | Cycle theme |
| `A` | Toggle auto-scroll |
| `[` / `]` | Decrease / increase auto-scroll speed |
| `N` / `P` | Next / previous chapter |
| `O` / `F` | Open folder / choose image files |
| `L` | Toggle the Library / History side panel |
| `D` | Open Discover & Download, if `gallery-dl` is installed |
| `C` | Continue reading |
| `Z` | Toggle zen mode |
| `F11` | Toggle fullscreen |
| `H` or double-click near the top of the reader | Toggle toolbar |
| `Esc` | Leave fullscreen/zen mode, return from browsing, or return to Home |
| `Backspace` | Return to Home |
| `Q` | Quit |

## Discover & Download notes

Discover & Download uses the installed `gallery-dl` extractors and supports four input modes: **Search**, **Codes**, **Tags**, and **URLs**. Available sources and capabilities depend on the installed `gallery-dl` version. Some sites may require a browser-exported cookies file and a matching user-agent, which can be configured from the settings button.

Tag search offers a rating-filtered-sites option. Not every supported site has a configured rating filter; the app warns before using unfiltered sites. A rating filter is not a guarantee of content safety. Review results and source policies before downloading. The app does not bundle `gallery-dl` or provide credentials for third-party sites.

## Settings and saved data

The app stores preferences, reading progress, history, and bookmarks in `~/.webtoon_reader.json` (the current user's home directory). Download cover thumbnails are cached under `~/.webtoon_reader_covers/`. The selected download folder and its `gallery-dl` archive are configured in Discover & Download settings.

## Supported image formats

PNG, JPG/JPEG, WebP, BMP, GIF, TIF, and TIFF.

## Troubleshooting

- **`Pillow is required`:** install it with `python -m pip install Pillow`.
- **Tkinter import error:** install the Tkinter package provided by your operating system or Python distribution.
- **Discover & Download is unavailable:** install or update `gallery-dl`, then restart the app.
- **A source returns no results or fails:** source availability can change; check `gallery-dl` support, cookies, user-agent settings, and the app's download log.

## License

No license file was supplied with the project. Unless the repository includes a license, all rights remain with the copyright holder; contact the project owner for permission to reuse or redistribute it.
