"""Matplotlib backend for the sandbox: plt.show() saves the figures instead of opening a window.

Selected with MPLBACKEND=module://mpl_autosave. Each plt.show() writes every open figure
to figure_1.png, figure_2.png, ... in the working directory (which the bot attaches to
the reply), then closes them, like closing the windows would.
"""

import itertools

from matplotlib._pylab_helpers import Gcf
from matplotlib.backend_bases import FigureManagerBase
from matplotlib.backends.backend_agg import FigureCanvasAgg

_numbers = itertools.count(1)


class FigureManager(FigureManagerBase):
    @classmethod
    def pyplot_show(cls, *, block=None):
        for manager in Gcf.get_all_fig_managers():
            manager.canvas.figure.savefig(f"figure_{next(_numbers)}.png", dpi=120, bbox_inches="tight")
        Gcf.destroy_all()


class FigureCanvas(FigureCanvasAgg):
    manager_class = FigureManager
