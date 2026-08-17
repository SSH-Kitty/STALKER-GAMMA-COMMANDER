"""Profiles page: create, edit, activate and delete CLI profiles."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..settings import CliProfile, cli_ok, run_config_command
from .common import info_label, make_card, section_label


class ProfilesPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.settings = window.settings

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        top = QHBoxLayout()
        top.setSpacing(16)
        list_card, list_layout = make_card()
        top.addWidget(list_card, 1)
        form_card, form_layout = make_card()
        top.addWidget(form_card, 2)
        root.addLayout(top)

        # ----- profile list -----
        list_layout.addWidget(section_label("Profiles"))
        list_layout.addWidget(
            info_label(
                "Each profile keeps its own Anomaly, GAMMA and cache folders "
                "plus download and repository settings. The active profile is "
                "what the other pages operate on."
            )
        )
        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(self._on_select)
        list_layout.addWidget(self.profile_list, 1)

        btn_row = QHBoxLayout()
        self.new_button = QPushButton("New")
        self.new_button.setToolTip("Start a blank profile form.")
        self.new_button.clicked.connect(self._new_profile)
        self.active_button = QPushButton("Set Active")
        self.active_button.setObjectName("primary")
        self.active_button.setToolTip(
            "Make the selected profile active so the other pages use it."
        )
        self.active_button.clicked.connect(self._set_active)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.setToolTip("Remove the selected profile.")
        self.delete_button.clicked.connect(self._delete_profile)
        for b in (self.new_button, self.active_button, self.delete_button):
            btn_row.addWidget(b)
        list_layout.addLayout(btn_row)

        # ----- form -----
        form_layout.addWidget(section_label("Profile Details"))
        form_layout.addWidget(
            info_label(
                "Anomaly, GAMMA and Cache paths are required. "
                "Hover a field for details."
            )
        )
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.name_edit = QLineEdit()
        self.anomaly_edit = QLineEdit()
        self.gamma_edit = QLineEdit()
        self.cache_edit = QLineEdit()
        self.mo2_edit = QLineEdit()
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 20)

        def path_row(edit: QLineEdit) -> QHBoxLayout:
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            browse = QPushButton("Browse...")
            browse.setToolTip("Pick the folder with a file dialog.")
            browse.clicked.connect(lambda: self._browse(edit))
            row.addWidget(browse)
            return row

        def field(label_text: str, widget: QWidget, tooltip: str) -> QLabel:
            label = QLabel(label_text)
            label.setToolTip(tooltip)
            widget.setToolTip(tooltip)
            return label

        self.form.addRow(
            field("Name", self.name_edit, "A short label used to identify this profile."),
            self.name_edit,
        )
        self.form.addRow(
            field(
                "Anomaly path",
                self.anomaly_edit,
                "The base S.T.A.L.K.E.R. Anomaly folder.",
            ),
            path_row(self.anomaly_edit),
        )
        self.form.addRow(
            field(
                "GAMMA path",
                self.gamma_edit,
                "The folder containing ModOrganizer.exe.",
            ),
            path_row(self.gamma_edit),
        )
        self.form.addRow(
            field(
                "Cache path",
                self.cache_edit,
                "A location with enough room for downloaded archives.",
            ),
            path_row(self.cache_edit),
        )
        self.form.addRow(
            field(
                "MO2 profile",
                self.mo2_edit,
                "Must match a profile inside the GAMMA folder's profiles/ "
                "directory (e.g. G.A.M.M.A). Creating a CLI profile does not "
                "create an MO2 profile.",
            ),
            self.mo2_edit,
        )
        self.form.addRow(
            field(
                "Download threads",
                self.threads_spin,
                "Higher values use more bandwidth and disk I/O.",
            ),
            self.threads_spin,
        )
        form_layout.addLayout(self.form)

        self.save_button = QPushButton("Create Profile")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save_or_create)
        form_layout.addWidget(self.save_button)

        # ----- advanced repositories & URLs -----
        self.modpack_edit = QLineEdit()
        self.modlist_edit = QLineEdit()
        self.gs_url = QLineEdit()
        self.gs_branch = QLineEdit()
        self.sg_url = QLineEdit()
        self.sg_branch = QLineEdit()
        self.glf_url = QLineEdit()
        self.glf_branch = QLineEdit()
        self.tg_url = QLineEdit()
        self.tg_branch = QLineEdit()

        advanced_card, adv_layout = make_card()
        root.addWidget(advanced_card)
        adv_layout.addWidget(section_label("Advanced: Repositories & URLs", level=2))
        adv_layout.addWidget(
            info_label(
                "Used to build the addon list. Only change these if you use a "
                "fork or mirror."
            )
        )
        repo_fields = [
            ("ModPackMaker", self.modpack_edit, None),
            ("ModList", self.modlist_edit, None),
            ("gamma_setup", self.gs_url, self.gs_branch),
            ("Stalker_GAMMA", self.sg_url, self.sg_branch),
            ("gamma_large_files", self.glf_url, self.glf_branch),
            ("teivaz_anomaly_gunslinger", self.tg_url, self.tg_branch),
        ]
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.addWidget(info_label("Repository"), 0, 0)
        grid.addWidget(info_label("URL"), 0, 1)
        grid.addWidget(info_label("Branch"), 0, 2)
        for row, (label, url_edit, branch_edit) in enumerate(repo_fields, start=1):
            name_label = QLabel(label)
            name_label.setObjectName("dim")
            grid.addWidget(name_label, row, 0)
            grid.addWidget(url_edit, row, 1)
            if branch_edit is not None:
                branch_edit.setMaximumWidth(150)
                grid.addWidget(branch_edit, row, 2)
        grid.setColumnStretch(1, 1)
        adv_layout.addLayout(grid)

        self._form_state = ""
        self.name_edit.textChanged.connect(self._update_save_button)
        self.refresh()

    # ----- list -----
    def refresh(self) -> None:
        self.window.refresh_settings()
        self.settings = self.window.settings
        previous = self._form_state
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for profile in self.settings.profiles:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_name)
            widget = self._profile_item_widget(profile)
            item.setSizeHint(widget.sizeHint())
            self.profile_list.addItem(item)
            self.profile_list.setItemWidget(item, widget)
        self.profile_list.blockSignals(False)

        has_profiles = self.profile_list.count() > 0
        for button in (self.active_button, self.delete_button):
            button.setEnabled(has_profiles)
        if has_profiles:
            # Keep the user on the profile they were editing across a save.
            names = [
                self.profile_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.profile_list.count())
            ]
            row = names.index(previous) if previous in names else 0
            self.profile_list.setCurrentRow(row)
        else:
            self._form_state = ""
            self._load_form(CliProfile())

    def _profile_item_widget(self, profile: CliProfile) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(1)
        name = QLabel(("●  " if profile.active else "") + profile.profile_name)
        if profile.active:
            name.setObjectName("accent")
        path = QLabel(profile.gamma or "No GAMMA path set")
        path.setObjectName("dim")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(name)
        layout.addWidget(path)
        return widget

    def _on_select(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        profile = next(
            (p for p in self.settings.profiles if p.profile_name == name),
            CliProfile(),
        )
        self._form_state = name
        self._load_form(profile)

    def _load_form(self, profile: CliProfile) -> None:
        self.name_edit.setText(profile.profile_name)
        self.anomaly_edit.setText(profile.anomaly)
        self.gamma_edit.setText(profile.gamma)
        self.cache_edit.setText(profile.cache)
        self.mo2_edit.setText(profile.mo2_profile)
        self.threads_spin.setValue(profile.download_threads)
        self.modpack_edit.setText(profile.mod_pack_maker_url)
        self.modlist_edit.setText(profile.mod_list_url)
        self.gs_url.setText(profile.gamma_setup_repo_url)
        self.gs_branch.setText(profile.gamma_setup_repo_branch)
        self.sg_url.setText(profile.stalker_gamma_repo_url)
        self.sg_branch.setText(profile.stalker_gamma_repo_branch)
        self.glf_url.setText(profile.gamma_large_files_repo_url)
        self.glf_branch.setText(profile.gamma_large_files_repo_branch)
        self.tg_url.setText(profile.teivaz_anomaly_gunslinger_repo_url)
        self.tg_branch.setText(profile.teivaz_anomaly_gunslinger_repo_branch)
        self._update_save_button()

    def _form_values(self) -> CliProfile:
        profile = CliProfile()
        profile.profile_name = self.name_edit.text().strip() or "gamma"
        profile.anomaly = self.anomaly_edit.text().strip()
        profile.gamma = self.gamma_edit.text().strip()
        profile.cache = self.cache_edit.text().strip()
        profile.mo2_profile = self.mo2_edit.text().strip() or "G.A.M.M.A"
        profile.download_threads = self.threads_spin.value()
        profile.mod_pack_maker_url = self.modpack_edit.text().strip() or profile.mod_pack_maker_url
        profile.mod_list_url = self.modlist_edit.text().strip() or profile.mod_list_url
        profile.gamma_setup_repo_url = self.gs_url.text().strip() or profile.gamma_setup_repo_url
        profile.gamma_setup_repo_branch = self.gs_branch.text().strip() or "main"
        profile.stalker_gamma_repo_url = self.sg_url.text().strip() or profile.stalker_gamma_repo_url
        profile.stalker_gamma_repo_branch = self.sg_branch.text().strip() or "main"
        profile.gamma_large_files_repo_url = (
            self.glf_url.text().strip() or profile.gamma_large_files_repo_url
        )
        profile.gamma_large_files_repo_branch = self.glf_branch.text().strip() or "main"
        profile.teivaz_anomaly_gunslinger_repo_url = (
            self.tg_url.text().strip() or profile.teivaz_anomaly_gunslinger_repo_url
        )
        profile.teivaz_anomaly_gunslinger_repo_branch = (
            self.tg_branch.text().strip() or "main"
        )
        return profile

    # ----- button state -----
    def _update_save_button(self) -> None:
        name = self.name_edit.text().strip()
        editing = any(p.profile_name == name for p in self.settings.profiles)
        if editing:
            self.save_button.setText("Save Changes")
            self.save_button.setToolTip("Save changes to the selected profile.")
        else:
            self.save_button.setText("Create Profile")
            self.save_button.setToolTip("Create a new profile and activate it.")

    def _save_or_create(self) -> None:
        name = self.name_edit.text().strip()
        exists = any(p.profile_name == name for p in self.settings.profiles)
        # Renaming onto an existing profile (or New keeping a name that already
        # exists) would silently overwrite that profile's data.
        if exists and name != self._form_state:
            QMessageBox.warning(
                self, "Name In Use",
                f"A profile named '{name}' already exists. Choose a different name.",
            )
            return
        if exists:
            self._form_state = name
            self._save_profile()
        else:
            self._create_profile()

    # ----- actions -----
    def _browse(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select folder", edit.text())
        if path:
            edit.setText(path)

    def _new_profile(self) -> None:
        self._form_state = ""
        self._load_form(CliProfile())
        # An empty name field: "New" must never prefill a default name that
        # could collide with an existing profile.
        self.name_edit.clear()
        self._update_save_button()
        self.name_edit.setFocus()

    def _create_profile(self) -> None:
        profile = self._form_values()
        if not (profile.anomaly and profile.gamma and profile.cache):
            QMessageBox.warning(self, "Missing Paths", "Anomaly, GAMMA and Cache paths are required.")
            return
        args = [
            "create",
            "--anomaly", profile.anomaly,
            "--gamma", profile.gamma,
            "--cache", profile.cache,
            "--name", profile.profile_name,
            "--mo2-profile", profile.mo2_profile,
            "--mod-pack-maker-url", profile.mod_pack_maker_url,
            "--mod-list-url", profile.mod_list_url,
            "--download-threads", str(profile.download_threads),
            "--gamma-setup-repo-url", profile.gamma_setup_repo_url,
            "--gamma-setup-repo-branch", profile.gamma_setup_repo_branch,
            "--stalker-gamma-repo-url", profile.stalker_gamma_repo_url,
            "--stalker-gamma-repo-branch", profile.stalker_gamma_repo_branch,
            "--gamma-large-files-repo-url", profile.gamma_large_files_repo_url,
            "--gamma-large-files-repo-branch", profile.gamma_large_files_repo_branch,
            "--teivaz-anomaly-gunslinger-repo-url", profile.teivaz_anomaly_gunslinger_repo_url,
            "--teivaz-anomaly-gunslinger-repo-branch",
            profile.teivaz_anomaly_gunslinger_repo_branch,
        ]
        rc, out, err = run_config_command(args, timeout=300)
        if not cli_ok(rc, out, err):
            QMessageBox.warning(
                self, "Create Failed", (out + "\n" + err).strip() or "config create failed"
            )
            return
        self.window.refresh_settings()
        if not any(p.profile_name == profile.profile_name for p in self.window.settings.profiles):
            QMessageBox.warning(self, "Create Failed", "The CLI did not create the profile.")
            return
        self.refresh()
        QMessageBox.information(self, "Created", f"Profile '{profile.profile_name}' created and activated.")

    def _save_profile(self) -> None:
        profile = self._form_values()
        if not (profile.anomaly and profile.gamma and profile.cache):
            QMessageBox.warning(
                self, "Missing Paths", "Anomaly, GAMMA and Cache paths are required."
            )
            return
        active = self.settings.active_profile
        existing = next(
            (p for p in self.settings.profiles if p.profile_name == profile.profile_name),
            None,
        )
        if existing is not None:
            # Keep any CLI-only keys this GUI does not model.
            profile.extra = dict(existing.extra)
            self.settings.profiles.remove(existing)
        # bool() is required: the `and` below yields '' for a new profile, and
        # the CLI deserializes Active into a C# bool and throws on a string.
        profile.active = bool(
            active is None
            or (existing is not None and existing.profile_name == active.profile_name)
        )
        self.settings.profiles.append(profile)
        try:
            self.settings.save()
        except OSError as exc:
            QMessageBox.warning(
                self, "Save Failed", f"Could not write settings.json:\n{exc}"
            )
            return
        self.window.refresh_settings()
        self.refresh()
        QMessageBox.information(self, "Saved", f"Profile '{profile.profile_name}' saved.")

    def _selected_profile_name(self) -> str | None:
        """Name of the highlighted profile, or None when the list is empty."""
        item = self.profile_list.currentItem()
        if item is None:
            QMessageBox.information(
                self, "No Selection", "Select a profile in the list first."
            )
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _set_active(self) -> None:
        name = self._selected_profile_name()
        if name is None:
            return
        rc, out, err = run_config_command(["use", name], timeout=300)
        if not cli_ok(rc, out, err):
            QMessageBox.warning(self, "Failed", (out + "\n" + err).strip() or "config use failed")
            return
        self.window.refresh_settings()
        active = self.window.settings.active_profile
        if active is None or active.profile_name != name:
            QMessageBox.warning(self, "Failed", f"Profile '{name}' could not be activated.")
            return
        self.refresh()
        QMessageBox.information(self, "Activated", f"Profile '{name}' is now active.")

    def _delete_profile(self) -> None:
        name = self._selected_profile_name()
        if name is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete Profile",
            f"Delete profile '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        rc, out, err = run_config_command(["delete", name], timeout=300)
        if not cli_ok(rc, out, err):
            QMessageBox.warning(self, "Failed", (out + "\n" + err).strip() or "config delete failed")
            return
        self.window.refresh_settings()
        if any(p.profile_name == name for p in self.window.settings.profiles):
            QMessageBox.warning(self, "Failed", f"Profile '{name}' could not be deleted.")
            return
        self.refresh()
