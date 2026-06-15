"""fit_on_screen keeps every window inside the visible work area: it must
shrink windows (even fixed-size ones) on small/scaled displays, center on
first show, and nudge off-screen windows back into view - without fighting a
position the user chose. Pure-logic tests with duck-typed fakes (no Qt)."""

import unittest


class FakePoint:
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class FakeRect:
    def __init__(self, x, y, w, h):
        self._x, self._y, self._w, self._h = x, y, w, h

    def left(self):
        return self._x

    def top(self):
        return self._y

    def right(self):
        return self._x + self._w - 1

    def bottom(self):
        return self._y + self._h - 1

    def width(self):
        return self._w

    def height(self):
        return self._h

    def center(self):
        return FakePoint(self._x + self._w // 2, self._y + self._h // 2)


class FakeScreen:
    def __init__(self, rect):
        self._rect = rect

    def availableGeometry(self):
        return self._rect


class FakeWin:
    def __init__(self, w, h, x=100, y=100, min_w=0, min_h=0, screen_rect=None):
        self._w, self._h, self._x, self._y = w, h, x, y
        self._min_w, self._min_h = min_w, min_h
        self._screen = FakeScreen(screen_rect or FakeRect(0, 0, 1920, 1040))

    def screen(self):
        return self._screen

    def minimumWidth(self):
        return self._min_w

    def minimumHeight(self):
        return self._min_h

    def setMinimumSize(self, w, h):
        self._min_w, self._min_h = w, h

    def width(self):
        return self._w

    def height(self):
        return self._h

    def resize(self, w, h):
        self._w = max(w, self._min_w)
        self._h = max(h, self._min_h)

    def x(self):
        return self._x

    def y(self):
        return self._y

    def move(self, x, y):
        self._x, self._y = x, y


class FakeFramedWin(FakeWin):
    """A window that, like a real shown window, reports an OS frame (title
    bar) around the client area via frameGeometry()."""

    def __init__(self, *args, frame_w=0, frame_h=32, **kwargs):
        super().__init__(*args, **kwargs)
        self._frame_w, self._frame_h = frame_w, frame_h

    def frameGeometry(self):
        return FakeRect(self._x, self._y,
                        self._w + self._frame_w, self._h + self._frame_h)


class TestFitOnScreen(unittest.TestCase):
    def _fit(self, win, **kw):
        from ui.winfit import fit_on_screen
        fit_on_screen(win, **kw)

    def _fully_visible(self, win):
        a = win.screen().availableGeometry()
        fw = fh = 0
        if hasattr(win, "frameGeometry"):
            fg = win.frameGeometry()
            fw, fh = fg.width() - win.width(), fg.height() - win.height()
        return (win.x() >= a.left() and win.y() >= a.top()
                and win.x() + win.width() + fw <= a.right() + 1
                and win.y() + win.height() + fh <= a.bottom() + 1)

    def test_oversized_fixed_window_shrinks_and_centers(self):
        # A 520x680 fixed-size window (min == size) on a 600px-tall work area:
        # without relaxing the minimum, resize() would silently refuse.
        win = FakeWin(520, 680, min_w=520, min_h=680,
                      screen_rect=FakeRect(0, 0, 1000, 600))
        self._fit(win)
        self.assertLessEqual(win.height(), 600 - 24)
        self.assertTrue(self._fully_visible(win))

    def test_first_show_centers(self):
        win = FakeWin(400, 300, x=-500, y=-500,
                      screen_rect=FakeRect(0, 0, 1000, 800))
        self._fit(win)
        self.assertEqual(win.x(), 500 - 200)
        self.assertEqual(win.y(), 400 - 150)

    def test_later_show_clamps_into_view_only(self):
        # Title bar above the top edge -> nudged down; size untouched.
        win = FakeWin(400, 300, x=50, y=-40,
                      screen_rect=FakeRect(0, 0, 1000, 800))
        win._fit_positioned = True
        self._fit(win)
        self.assertEqual((win.x(), win.y()), (50, 0))
        self.assertEqual((win.width(), win.height()), (400, 300))

    def test_user_position_left_alone_when_visible(self):
        win = FakeWin(400, 300, x=123, y=77,
                      screen_rect=FakeRect(0, 0, 1000, 800))
        win._fit_positioned = True
        self._fit(win)
        self.assertEqual((win.x(), win.y()), (123, 77))

    def test_taskbar_offset_work_area_respected(self):
        # Work area starting below a top-docked taskbar (y=40).
        win = FakeWin(400, 300, x=0, y=0, screen_rect=FakeRect(0, 40, 1000, 760))
        win._fit_positioned = True
        self._fit(win)
        self.assertGreaterEqual(win.y(), 40)

    def test_title_bar_never_pushes_bottom_off_screen(self):
        # The reported bug: a 1536x816 work area (1080p laptop at 125%) and a
        # window stretched to near the cap. The old math capped the CLIENT
        # height to the work area but moved by FRAME coordinates, so the
        # 32px title bar hung the bottom edge ~20px below the screen.
        win = FakeFramedWin(640, 792, x=448, y=12,
                            screen_rect=FakeRect(0, 0, 1536, 816))
        win._fit_positioned = True
        self._fit(win)
        self.assertTrue(self._fully_visible(win))

    def test_frame_counted_when_centering(self):
        # First show of an oversized window: shrink + center must keep the
        # whole frame (not just the client area) inside the work area.
        win = FakeFramedWin(900, 700, min_w=900, min_h=700,
                            screen_rect=FakeRect(0, 0, 1000, 600))
        self._fit(win)
        self.assertLessEqual(win.height() + 32, 600 - 24 + 32)  # client capped
        self.assertTrue(self._fully_visible(win))

    def test_first_show_top_clamped_into_view(self):
        # The reported bug: on first show the window is taller than the work
        # area (layout not settled / frame size unknown), so centering would
        # put its top above the screen. The clamp must pull the top back to
        # the work-area top so the title bar is always reachable.
        win = FakeFramedWin(600, 900, min_h=900,
                            screen_rect=FakeRect(0, 40, 1000, 760))
        self.assertFalse(getattr(win, "_fit_positioned", False))  # first show
        self._fit(win)
        self.assertGreaterEqual(win.y(), 40)  # top never above the work area

    def test_size_to_screen_leaves_room_for_frame(self):
        # The proportional default size must subtract the frame too, so the
        # proposal already fits with its title bar on small screens.
        from ui.winfit import size_to_screen
        win = FakeFramedWin(720, 780, screen_rect=FakeRect(0, 0, 1000, 580))
        size_to_screen(win, 0.5, 0.95, 400, 540, 800, 880)
        a = win.screen().availableGeometry()
        self.assertLessEqual(win.height() + 32, a.height() - 24)


if __name__ == "__main__":
    unittest.main()
