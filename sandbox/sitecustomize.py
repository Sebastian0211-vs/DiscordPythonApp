"""Loaded automatically by Python at startup in the sandbox (it is on PYTHONPATH).

In plain scripts (REMOTEPY_SCRIPT=1) there is no screen and no notebook, so "show" calls
would do nothing useful. This makes them save a PNG in the working directory instead,
which the bot attaches to the reply:

- PIL:     img.show()                      -> image_1.png, image_2.png, ...
- IPython: display(img) / display(fig)     -> image_N.png / figure_N.png (other objects print as usual)
- matplotlib plt.show() is handled by the mpl_autosave backend.

In both scripts and notebooks, interactive figures are saved as standalone HTML pages
(plotly_1.html, altair_1.html, bokeh_1.html; the chart libraries load from their CDNs,
which keeps each page a few KB instead of several MB).
The bot publishes them as web pages and posts links: fig.show() / a figure as the last
line of a notebook cell (Plotly), chart.show() / a chart as last line (Altair), show(p) (Bokeh).

In both scripts and notebooks, pandas Styler tables (df.style.background_gradient(...))
get a PNG rendering, since Discord can't show their HTML: colored table images in
notebook cells, and display(styler) saves table_N.png in scripts.

Nothing is imported up front: the patches are applied only when the script itself
imports PIL.ImageShow or IPython, so scripts that don't use them start as fast as before.
In notebooks (REMOTEPY_NOTEBOOK=1) img.show() is sent to Jupyter's inline display,
even if some desktop image viewer happens to be installed in the image.
"""

import os
import sys

_MODE = "script" if os.environ.get("REMOTEPY_SCRIPT") == "1" else (
    "notebook" if os.environ.get("REMOTEPY_NOTEBOOK") == "1" else None)

if _MODE:
    import itertools
    from importlib.abc import MetaPathFinder

    _counters: dict[str, itertools.count] = {}

    def _next_name(prefix: str, ext: str = ".png") -> str:
        counter = _counters.setdefault(prefix, itertools.count(1))
        while True:
            name = f"{prefix}_{next(counter)}{ext}"
            if not os.path.exists(name):
                return name

    def _save_pil(image) -> None:
        if image.mode not in ("1", "L", "LA", "I", "I;16", "P", "RGB", "RGBA"):
            image = image.convert("RGBA" if "A" in image.mode else "RGB")
        image.save(_next_name("image"), format="PNG")

    def _patch_imageshow(module) -> None:
        if _MODE == "notebook":
            if hasattr(module, "IPythonViewer"):
                module.register(module.IPythonViewer, order=-1)
            return

        class SaveToFileViewer(module.Viewer):
            def show(self, image, **options):
                _save_pil(image)
                return 1

        module.register(SaveToFileViewer, order=-1)  # before the IPython and X11 viewers

    def _styler_png(styler, max_rows: int = 60, max_cols: int = 20) -> bytes:
        import io

        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.colors import to_rgba
        from matplotlib.figure import Figure

        styler._compute()
        data = styler.data
        rows = [r for r in range(len(data.index)) if r not in set(styler.hidden_rows)][:max_rows]
        cols = [c for c in range(len(data.columns)) if c not in set(styler.hidden_columns)][:max_cols]

        def fmt(r, c):
            try:
                return str(styler._display_funcs[(r, c)](data.iat[r, c]))
            except Exception:
                return str(data.iat[r, c])

        def css(r, c):
            props = dict(styler.ctx.get((r, c), []))
            out = {}
            for key, target in (("background-color", "bg"), ("color", "fg")):
                try:
                    out[target] = to_rgba(props[key])
                except (KeyError, ValueError):
                    pass
            return out

        text = [[fmt(r, c) for c in cols] for r in rows]
        styles = [[css(r, c) for c in cols] for r in rows]
        row_labels = [str(data.index[r]) for r in rows]
        col_labels = [str(data.columns[c]) for c in cols]

        width = max(len(t) for t in [*col_labels, *(x for row in text for x in row), "0"])
        label_w = max((len(x) for x in row_labels), default=1)
        fig = Figure(figsize=(min(0.12 * (width + 2) * len(cols) + 0.1 * label_w + 0.5, 30),
                              min(0.32 * (len(rows) + 1) + 0.3, 30)))
        FigureCanvasAgg(fig)
        ax = fig.add_subplot()
        ax.axis("off")
        table = ax.table(cellText=text or [[""]], rowLabels=row_labels or None, colLabels=col_labels or None,
                         cellLoc="center", loc="center")
        table.auto_set_font_size(False)
        table.set_fontsize(9)
        for (i, j), cell in table.get_celld().items():
            cell.set_edgecolor("#d0d0d0")
            if i == 0 or j == -1:  # header row / index column
                cell.set_text_props(weight="bold")
                cell.set_facecolor("#f2f2f2")
                continue
            st = styles[i - 1][j]
            if "bg" in st:
                cell.set_facecolor(st["bg"])
            if "fg" in st:
                cell.get_text().set_color(st["fg"])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        return buf.getvalue()

    def _patch_styler(module) -> None:
        def _repr_png_(self):
            try:
                return _styler_png(self)
            except Exception:
                return None  # fall back to the other representations

        module.Styler._repr_png_ = _repr_png_

    def _patch_display(module) -> None:
        original = module.display

        def display(*objs, **kwargs):
            rest = []
            for obj in objs:
                kind = type(obj).__module__
                if kind.startswith("PIL.") and hasattr(obj, "save") and hasattr(obj, "mode"):
                    _save_pil(obj)
                elif kind.startswith("matplotlib.figure"):
                    obj.savefig(_next_name("figure"), dpi=120, bbox_inches="tight")
                elif kind.startswith("pandas.io.formats.style") and hasattr(obj, "_repr_png_"):
                    png = obj._repr_png_()
                    if png:
                        with open(_next_name("table"), "wb") as f:
                            f.write(png)
                    else:
                        rest.append(obj)
                else:
                    rest.append(obj)
            if rest or not objs:
                return original(*rest, **kwargs)

        module.display = display

    def _interactive_saved(name: str) -> None:
        print(f"[interactive plot: {name}]", flush=True)

    def _patch_plotly(module) -> None:
        from plotly.io._base_renderers import ExternalRenderer

        class SaveHtmlRenderer(ExternalRenderer):
            def render(self, fig_dict):
                name = _next_name("plotly", ".html")
                module.write_html(fig_dict, name, include_plotlyjs="cdn", full_html=True, auto_open=False)
                _interactive_saved(name)

        module.renderers["remotepy"] = SaveHtmlRenderer()
        module.renderers.default = "remotepy"

    def _patch_altair(module) -> None:
        def save_chart(spec) -> str:
            name = _next_name("altair", ".html")
            module.Chart.from_dict(spec).save(name, format="html")
            return f"[interactive plot: {name}]"

        def renderer(spec, **metadata):
            return {"text/plain": save_chart(spec)}  # shown as the cell's result, no print needed

        module.renderers.register("remotepy", renderer)
        module.renderers.enable("remotepy")

        mixin = next(c for c in module.Chart.__mro__ if c.__name__ == "TopLevelMixin")

        def show(self, *args, **kwargs):
            print(save_chart(self.to_dict()), flush=True)

        mixin.show = show

    def _patch_bokeh(module) -> None:
        def show(obj, browser=None, new="tab", notebook_handle=False, notebook_url=None, **kwargs):
            from bokeh.io import save
            from bokeh.resources import CDN

            name = _next_name("bokeh", ".html")
            save(obj, filename=name, resources=CDN, title=name)
            _interactive_saved(name)

        module.show = show

    _HOOKS = {
        "PIL.ImageShow": _patch_imageshow,
        "pandas.io.formats.style": _patch_styler,
        "plotly.io": _patch_plotly,
        "altair": _patch_altair,
        "bokeh.io.showing": _patch_bokeh,
    }
    if _MODE == "script":
        _HOOKS["IPython.core.display_functions"] = _patch_display

    class _PatchOnImport(MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            hook = _HOOKS.get(name)
            if hook is None:
                return None
            for finder in sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(name, path, target)
                if spec is not None and spec.loader is not None:
                    exec_module = spec.loader.exec_module

                    def patched(module, _exec=exec_module, _hook=hook):
                        _exec(module)
                        try:
                            _hook(module)
                        except Exception:  # never break the user's import
                            pass

                    spec.loader.exec_module = patched
                    return spec
            return None

    sys.meta_path.insert(0, _PatchOnImport())
