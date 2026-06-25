# Static assets (served at the site root)

Files in this folder are served as-is at the root path. For example,
`public/ocean.jpg` is reachable at `/ocean.jpg`.

## Login background image

The login screen's right panel shows a self-contained emerald-ocean SVG by
default. To use your own photo instead, **add an image here named exactly
`ocean.jpg`**:

```
webui/frontend/public/ocean.jpg
```

No code change is needed — the login CSS already references `/ocean.jpg`.
When the file is present it shows through the brand-green overlay; when it's
absent, the request simply 404s and the default ocean art shows instead.

Tip: a tall (portrait) image works best, since the panel is full-height. JPG
or PNG are both fine — if you use PNG, name it `ocean.jpg` anyway or update
the `url('/ocean.jpg')` reference in `src/styles.css` (`.login__art-photo`).
