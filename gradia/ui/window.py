# Copyright (C) 2025 Alexander Vanhee, tfuxu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from collections.abc import Callable
import os
import threading
from typing import Any, Optional

from gi.repository import Adw, GLib, GObject, Gdk, Gio, Gtk, Xdp

from gradia.clipboard import *
from gradia.graphics.background import Background
from gradia.graphics.gradient import GradientBackground
from gradia.graphics.image import ImageBackground
from gradia.graphics.image_processor import ImageProcessor
from gradia.graphics.solid import SolidBackground
from gradia.overlay.drawing_actions import DrawingMode
from gradia.ui.background_selector import BackgroundSelector
from gradia.ui.image_exporters import ExportManager
from gradia.ui.image_loaders import ImportManager
from gradia.ui.image_sidebar import ImageSidebar
from gradia.ui.ui_parts import *
from gradia.ui.welcome_page import WelcomePage
from gradia.utils.aspect_ratio import *


class GradientWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'GradientWindow'

    DEFAULT_WINDOW_WIDTH: int = 975
    DEFAULT_WINDOW_HEIGHT: int = 675
    DEFAULT_PANED_POSITION: int = 650
    SIDEBAR_WIDTH: int = 300

    PAGE_IMAGE: str = "image"
    PAGE_LOADING: str = "loading"

    TEMP_PROCESSED_FILENAME: str = "processed.png"

    def __init__(
        self,
        temp_dir: str,
        version: str,
        init_screenshot_mode: Optional[Xdp.ScreenshotFlags],
        file_path: Optional[str] = None,
        **kwargs
    ) -> None:
        super().__init__(**kwargs)

        self.app: Adw.Application = kwargs['application']
        self.temp_dir: str = temp_dir
        self.version: str = version
        self.image_path: Optional[str] = None
        self.processed_path: Optional[str] = None
        self.processed_pixbuf: Optional[Gdk.Pixbuf] = None

        self.export_manager: ExportManager = ExportManager(self, temp_dir)
        self.import_manager: ImportManager = ImportManager(self, temp_dir, self.app)

        self.background_selector: BackgroundSelector = BackgroundSelector(
            gradient=GradientBackground(),
            solid=SolidBackground(),
            image=ImageBackground(),
            callback=self._on_background_changed,
            window=self
        )

        self.processor: ImageProcessor = ImageProcessor(
            padding=5,
            background=self.background_selector.get_current_background()
        )

        self.create_action("shortcuts", self._on_shortcuts_activated)
        self.create_action("about", self._on_about_activated)
        self.create_action('quit', lambda *_: self.app.quit(), ['<primary>q'])
        self.create_action("shortcuts", self._on_shortcuts_activated,  ['<primary>question'])

        self.create_action("open", lambda *_: self.import_manager.open_file_dialog(), ["<Primary>o"])
        self.create_action("load-drop", self.import_manager._on_drop_action, vt="s")
        self.create_action("paste", lambda *_: self.import_manager.load_from_clipboard(), ["<Primary>v"])
        self.create_action("screenshot", lambda *_: self.import_manager.take_screenshot(), ["<Primary>a"])
        self.create_action("open-path", lambda action, param: self.import_manager.load_from_file(param.get_string()), vt="s")

        self.create_action(
            "open-path-with-gradient",
            lambda action, param: (
                self.import_manager.load_from_file(param.unpack()[0]),
                setattr(self.processor, 'background', GradientBackground.fromIndex(param.unpack()[1]))
            ),
            vt="(si)"
        )

        self.create_action("save", lambda *_: self.export_manager.save_to_file(), ["<Primary>s"], enabled=False)
        self.create_action("copy", lambda *_: self.export_manager.copy_to_clipboard(), ["<Primary>c"], enabled=False)

        self.create_action("quit", lambda *_: self.close(), ["<Primary>q"])

        self.create_action("undo", lambda *_: self.drawing_overlay.undo(), ["<Primary>z"])
        self.create_action("redo", lambda *_: self.drawing_overlay.redo(), ["<Primary><Shift>z"])
        self.create_action("clear", lambda *_: self.drawing_overlay.clear_drawing())
        self.create_action("draw-mode", lambda action, param: self.drawing_overlay.set_drawing_mode(DrawingMode(param.get_string())), vt="s")

        self.create_action("pen-color", lambda action, param: self._set_pen_color_from_string(param.get_string()), vt="s")
        self.create_action("fill-color", lambda action, param: self._set_fill_color_from_string(param.get_string()), vt="s")
        self.create_action("highlighter-color", lambda action, param: self._set_highlighter_color_from_string(param.get_string()), vt="s")
        self.create_action("del-selected", lambda *_: self.drawing_overlay.remove_selected_action(), ["<Primary>x", "Delete"])
        self.create_action("font", lambda action, param: self.drawing_overlay.set_font_family(param.get_string()), vt="s")
        self.create_action("pen-size", lambda action, param: self.drawing_overlay.set_pen_size(param.get_double()), vt="d")
        self.create_action("number-radius", lambda action, param: self.drawing_overlay.set_number_radius(param.get_double()), vt="d")

        self.create_action("delete-screenshots", lambda *_: self._create_delete_screenshots_dialog(), enabled=False)

        self.file_path = file_path

        if init_screenshot_mode is not None:
            def screenshot_error_callback(error_message: str) -> None:
                 self.app.quit()

            def screenshot_success_callback() -> None:
                self.show()

            self.import_manager.take_screenshot(
                init_screenshot_mode,
                screenshot_error_callback,
                screenshot_success_callback
            )

    def create_action(
        self,
        name: str,
        callback: Callable[..., Any],
        shortcuts: Optional[list[str]] = None,
        enabled: bool = True,
        vt: Optional[str] = None
    ) -> None:
        variant_type = GLib.VariantType.new(vt) if vt is not None else None

        action: Gio.SimpleAction = Gio.SimpleAction.new(name, variant_type)
        action.connect("activate", callback)
        action.set_enabled(enabled)
        self.app.add_action(action)

        if shortcuts:
            self.app.set_accels_for_action(f"app.{name}", shortcuts)

    def _update_and_process(
        self,
        obj: Any,
        attr: str,
        transform: Callable[[Any], Any] = lambda x: x,
        assign_to: Optional[str] = None
    ) -> Callable[[Any], None]:
        def handler(widget: Any) -> None:
            value = transform(widget)
            setattr(obj, attr, value)

            if assign_to:
                setattr(self.processor, assign_to, obj)

            self._trigger_processing()

        return handler

    def build_ui(self) -> None:
        self._setup_window()
        self._setup_toolbar()
        self._setup_image_stack()
        self._setup_sidebar()
        self._setup_main_layout()

        if self.file_path:
            self.import_manager.load_from_file(self.file_path)

    def _setup_window(self) -> None:
        self.set_title("Gradia")
        self.set_default_size(self.DEFAULT_WINDOW_WIDTH, self.DEFAULT_WINDOW_HEIGHT)
        self.toast_overlay: Adw.ToastOverlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

    def _setup_toolbar(self) -> None:
        self.toolbar_view: Adw.ToolbarView = Adw.ToolbarView()
        self.toolbar_view.set_top_bar_style(Adw.ToolbarStyle.FLAT)

    def _setup_image_stack(self) -> None:
        stack_info = create_image_stack()
        self.image_stack: Gtk.Stack = stack_info[0]
        self.picture: Gtk.Picture = stack_info[1]
        self.spinner: Gtk.Widget = stack_info[2]
        self.drawing_overlay = stack_info[3]
        self.controls_overlay = stack_info[4]
        self.stack_box = stack_info[5]

    def _setup_sidebar(self) -> None:
        self.sidebar = ImageSidebar(
            background_selector_widget=self.background_selector,
            on_padding_changed=self.on_padding_changed,
            on_corner_radius_changed=self.on_corner_radius_changed,
            on_aspect_ratio_changed=self.on_aspect_ratio_changed,
            on_shadow_strength_changed=self.on_shadow_strength_changed
        )

        self.sidebar.set_size_request(self.SIDEBAR_WIDTH, -1)
        self.sidebar.set_visible(False)

    def _setup_main_layout(self) -> None:
        self.top_stack: Gtk.Stack = Gtk.Stack.new()
        self.top_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.top_stack.set_transition_duration(200)

        welcome_page = WelcomePage()
        self.top_stack.add_named(welcome_page, "empty")

        self.main_box: Gtk.Box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.main_box.set_vexpand(True)

        self.main_box.append(self.sidebar)
        self.main_box.append(self.stack_box)

        self.image_stack.set_hexpand(True)
        self.sidebar.set_hexpand(False)

        self.top_stack.add_named(self.main_box, "main")

        self.toolbar_view.set_content(self.top_stack)
        self.toast_overlay.set_child(self.toolbar_view)

    def show(self) -> None:
        self.present()

    def _start_processing(self) -> None:
        self.toolbar_view.set_top_bar_style(Adw.ToolbarStyle.RAISED)

        self.image_stack.get_style_context().add_class("view")
        self._show_loading_state()
        self.process_image()
        self._set_save_and_toggle(True)

    def _show_loading_state(self) -> None:
        self.top_stack.set_visible_child_name("main")
        self.image_stack.set_visible_child_name(self.PAGE_LOADING)

    def _hide_loading_state(self) -> None:
        self.image_stack.set_visible_child_name(self.PAGE_IMAGE)

    def _update_sidebar_file_info(self, filename: str, location: str) -> None:
        self.sidebar.filename_row.set_subtitle(filename)
        self.sidebar.location_row.set_subtitle(location)
        self.sidebar.set_visible(True)

    def _on_background_changed(self, updated_background: Background) -> None:
        self.processor.background = updated_background
        self._trigger_processing()

    def on_padding_changed(self, row: Adw.SpinRow) -> None:
        setattr(self.processor, "padding", int(row.get_value()))
        self._trigger_processing()

    def on_corner_radius_changed(self, row: Adw.SpinRow) -> None:
        setattr(self.processor, "corner_radius", int(row.get_value()))
        self._trigger_processing()

    def on_aspect_ratio_changed(self, entry: Gtk.Entry) -> None:
        text: str = entry.get_text().strip()
        try:
            ratio: Optional[float] = parse_aspect_ratio(text)
            if ratio is None:
                self.processor.aspect_ratio = None
                self._trigger_processing()
                return

            if not check_aspect_ratio_bounds(ratio):
                raise ValueError(f"Aspect ratio must be between 0.2 and 5 (got {ratio})")

            self.processor.aspect_ratio = ratio
            self._trigger_processing()

        except Exception as e:
            print(f"Invalid aspect ratio: {text} ({e})")

    def on_shadow_strength_changed(self, strength: Gtk.Scale) -> None:
        self.processor.shadow_strength = strength.get_value()
        self._trigger_processing()

    def _parse_rgba(self, color_string: str):
        return map(float, color_string.split(','))

    def _set_pen_color_from_string(self, color_string: str) -> None:
        self.drawing_overlay.set_pen_color(*self._parse_rgba(color_string))

    def _set_fill_color_from_string(self, color_string: str) -> None:
        self.drawing_overlay.set_fill_color(*self._parse_rgba(color_string))

    def _set_highlighter_color_from_string(self, color_string: str) -> None:
        self.drawing_overlay.set_highlighter_color(*self._parse_rgba(color_string))

    def _trigger_processing(self) -> None:
        if self.image_path:
            self.process_image()

    def process_image(self) -> None:
        if not self.image_path:
            return

        threading.Thread(target=self._process_in_background, daemon=True).start()

    def _process_in_background(self) -> None:
        try:
            if self.image_path is not None:
                self.processor.set_image_path(self.image_path)
                pixbuf: Gdk.Pixbuf = self.processor.process()
                self.processed_pixbuf = pixbuf
                self.processed_path = os.path.join(self.temp_dir, self.TEMP_PROCESSED_FILENAME)
                pixbuf.savev(self.processed_path, "png", [], [])
            else:
                print("No image path set for processing.")

            GLib.idle_add(self._update_image_preview, priority=GLib.PRIORITY_DEFAULT)  # pyright: ignore
        except Exception as e:
            print(f"Error processing image: {e}")

    def _update_image_preview(self) -> bool:
        if self.processed_pixbuf:
            paintable: Gdk.Paintable = Gdk.Texture.new_for_pixbuf(self.processed_pixbuf)
            self.picture.set_paintable(paintable)
            self._update_processed_image_size()
            self._hide_loading_state()
        return False

    def _update_processed_image_size(self) -> None:
        try:
            if self.processed_pixbuf:
                width: int = self.processed_pixbuf.get_width()
                height: int = self.processed_pixbuf.get_height()
                size_str: str = f"{width}×{height}"
                self.sidebar.processed_size_row.set_subtitle(size_str)
            else:
                self.sidebar.processed_size_row.set_subtitle(_("Unknown"))
        except Exception as e:
            self.sidebar.processed_size_row.set_subtitle(_("Error"))
            print(f"Error getting processed image size: {e}")

    def _show_notification(self, message: str) -> None:
        if self.toast_overlay:
            toast: Adw.Toast = Adw.Toast.new(message)
            self.toast_overlay.add_toast(toast)

    def _set_loading_state(self, is_loading: bool) -> None:
        if is_loading:
            self._show_loading_state()
        else:
            child: str = getattr(self, "_previous_stack_child", self.PAGE_IMAGE)
            self.image_stack.set_visible_child_name(child)

    def _on_about_activated(self, _action: Gio.SimpleAction, _param: GObject.ParamSpec) -> None:
        about = create_about_dialog(version=self.version)
        about.present(self)

    def _set_save_and_toggle(self, enabled: bool) -> None:
        for action_name in ["save", "copy"]:
            action = self.app.lookup_action(action_name)
            if action:
                action.set_enabled(enabled)

    def _on_shortcuts_activated(self, _action: Gio.SimpleAction, _param: GObject.ParamSpec) -> None:
        shortcuts = create_shortcuts_dialog(self)
        shortcuts.connect("close-request", self._on_shortcuts_closed)
        shortcuts.present()

    def _on_shortcuts_closed(self, dialog: Adw.Window) -> bool:
        dialog.hide()
        return True

    def _create_delete_screenshots_dialog(self) -> None:
        screenshot_uris = self.import_manager.get_screenshot_uris()
        if not screenshot_uris:
            self._show_notification(_("No screenshots to delete"))
            return

        filenames = [
            GLib.filename_display_basename(
                GLib.uri_parse(uri, GLib.UriFlags.NONE).get_path()
            )
            for uri in screenshot_uris
        ]


        file_list = Gtk.ListBox()
        file_list.set_selection_mode(Gtk.SelectionMode.NONE)
        file_list.add_css_class("boxed-list")


        for filename in filenames:
            row = Adw.ActionRow()
            label = Gtk.Label(label=filename, xalign=0)
            label.set_wrap(True)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(12)
            label.set_margin_end(12)
            row.set_child(label)
            file_list.append(row)

        count = len(screenshot_uris)
        if count == 1:
            heading = _("Delete Screenshot?")
            body = _("Are you sure you want to delete the following file?")
        else:
            heading = _("Delete Screenshots?")
            body = _("Are you sure you want to delete the following files?")

        dialog = Adw.AlertDialog(
            heading=heading,
            body=body,
            close_response="cancel"
        )

        dialog.set_extra_child(file_list)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog: Adw.AlertDialog, task: Gio.Task) -> None:
            response = dialog.choose_finish(task)
            if response == "delete":
                self.import_manager.delete_screenshots()
                if count == 1:
                    self._show_notification(_("Screenshot deleted"))
                else:
                    self._show_notification(_("Screenshots deleted"))

        dialog.choose(self, None, on_response)

    def _on_settings_activated(self, action: Gio.SimpleAction, param) -> None:
        settings_window = SettingsWindow(self)
        settings_window.present()

    def set_screenshot_subfolder(self, subfolder) -> None:
        Settings().screenshot_subfolder = subfolder
        self.welcome_page.refresh_recent_picker()
