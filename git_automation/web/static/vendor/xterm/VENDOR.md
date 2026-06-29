# Vendored: xterm.js

- **Package:** `@xterm/xterm`
- **Version:** `5.5.0` (pinned)
- **License:** MIT — © The xterm.js authors / SourceLair / Christopher Jeffrey
- **Source:** https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz
- **Files vendored for offline use:**
  - `xterm.js`  — from the package's `lib/xterm.js` (UMD build).
  - `xterm.css` — from the package's `css/xterm.css`.

The app is fully offline: nothing here is fetched from a CDN at runtime.
`static/js/terminal.js` loads `xterm.js` via a dynamically injected classic
`<script>` tag and `xterm.css` via a dynamically injected `<link>`. The UMD
build attaches the `Terminal` constructor to `window.Terminal`.

## Updating

Replace `xterm.js` / `xterm.css` with a newer build's `lib/xterm.js` /
`css/xterm.css`, then bump the version above. No code changes are required as
long as the UMD build keeps exposing `window.Terminal`.

## MIT License

```
Copyright (c) 2017-2019, The xterm.js authors (https://github.com/xtermjs/xterm.js)
Copyright (c) 2014-2016, SourceLair Private Company (https://www.sourcelair.com)
Copyright (c) 2012-2013, Christopher Jeffrey (https://github.com/chjj/)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
