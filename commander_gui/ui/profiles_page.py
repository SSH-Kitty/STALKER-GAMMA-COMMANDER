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

from ..modlist import seed_new_mo2_profile
from ..settings import CliProfile, cli_ok, run_config_command
from .common import (
    BackgroundTask,
    info_label,
    make_card,
    mo2_running,
    normalize_path,
    section_label,
    tr,
)


class ProfilesPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.settings = window.settings
        self._task: BackgroundTask | None = None
        self._browse_buttons: list[QPushButton] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("pageContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        scroll.setWidget(content)

        title = section_label(tr("PROFILES"), level=1)
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(title)
        subtitle = info_label(
            tr("Create and manage COMMANDER profiles. Each one keeps its Anomaly, GAMMA, and download-cache folders.")
        )
        subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        root.addWidget(subtitle)

        top = QHBoxLayout()
        top.setSpacing(16)
        list_card, list_layout = make_card()
        top.addWidget(list_card, 1)
        form_card, form_layout = make_card()
        form_layout.setContentsMargins(16, 2, 16, 16)
        top.addWidget(form_card, 2)
        root.addLayout(top)

        # ----- profile list -----
        list_layout.addWidget(section_label(tr("COMMANDER profiles")))
        list_layout.addWidget(
            info_label(
                tr("A COMMANDER profile stores install folders, download settings, and repository options. The active profile is what the other pages use.")
            )
        )
        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(self._on_select)
        list_layout.addWidget(self.profile_list, 1)

        btn_row = QHBoxLayout()
        self.new_button = QPushButton(tr("New profile"))
        self.new_button.setToolTip(tr("Start a blank COMMANDER profile form."))
        self.new_button.clicked.connect(self._new_profile)
        self.active_button = QPushButton(tr("Set active"))
        self.active_button.setObjectName("primary")
        self.active_button.setToolTip(
            tr("Make the selected COMMANDER profile active for the other pages.")
        )
        self.active_button.clicked.connect(self._set_active)
        self.delete_button = QPushButton(tr("Delete profile"))
        self.delete_button.setObjectName("danger")
        self.delete_button.setToolTip(tr("Remove the selected profile."))
        self.delete_button.clicked.connect(self._delete_profile)
        for b in (self.new_button, self.active_button, self.delete_button):
            btn_row.addWidget(b)
        list_layout.addLayout(btn_row)

        # ----- form -----
        form_layout.addWidget(section_label(tr("Profile details")))
        form_layout.addWidget(
            info_label(
                tr("Anomaly, GAMMA, and cache folders are required. Hover a field for details.")
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
            browse = QPushButton(tr("Browse..."))
            browse.setToolTip(tr("Pick the folder with a file dialog."))
            browse.clicked.connect(lambda: self._browse(edit))
            self._browse_buttons.append(browse)
            row.addWidget(browse)
            return row

        def field(label_text: str, widget: QWidget, tooltip: str) -> QLabel:
            label = QLabel(label_text)
            label.setToolTip(tooltip)
            widget.setToolTip(tooltip)
            return label

        self.form.addRow(
            field(
                "Name", self.name_edit, "A short label used to identify this profile."
            ),
            self.name_edit,
        )
        self.form.addRow(
            field(
                "Anomaly path",
                self.anomaly_edit,
                "The folder containing the STALKER Anomaly base game.",
            ),
            path_row(self.anomaly_edit),
        )
        self.form.addRow(
            field(
                "GAMMA path",
                self.gamma_edit,
                "The GAMMA folder containing ModOrganizer.exe.",
            ),
            path_row(self.gamma_edit),
        )
        self.form.addRow(
            field(
                "Cache path",
                self.cache_edit,
                "A location with enough free space for downloaded addon archives.",
            ),
            path_row(self.cache_edit),
        )
        self.form.addRow(
            field(
                "MO2 profile",
                self.mo2_edit,
                "The MO2 profile directory inside GAMMA/profiles (for example, "
                "G.A.M.M.A). Creating a COMMANDER profile does not create an MO2 profile.",
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
        form_layout.addWidget(
            info_label(
                tr("Tip: 4 threads = safe on slow connections, 6 = balanced, 8 = fast on good connections (may timeout on slow networks).")
            )
        )

        self.save_button = QPushButton(tr("Create profile"))
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
        adv_layout.addWidget(section_label(tr("Advanced: Repositories & URLs"), level=2))
        adv_layout.addWidget(
            info_label(
                tr("Used to build the addon list. Only change these if you use a fork or mirror.")
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
        grid.addWidget(info_label(tr("Repository")), 0, 0)
        grid.addWidget(info_label(tr("URL")), 0, 1)
        grid.addWidget(info_label(tr("Branch")), 0, 2)
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
        self._update_busy_state()

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
        profile.profile_name = self.name_edit.text().strip()
        profile.anomaly = normalize_path(self.anomaly_edit.text())
        profile.gamma = normalize_path(self.gamma_edit.text())
        profile.cache = normalize_path(self.cache_edit.text())
        profile.mo2_profile = self.mo2_edit.text().strip() or "G.A.M.M.A"
        profile.download_threads = self.threads_spin.value()
        profile.mod_pack_maker_url = (
            self.modpack_edit.text().strip() or profile.mod_pack_maker_url
        )
        profile.mod_list_url = self.modlist_edit.text().strip() or profile.mod_list_url
        profile.gamma_setup_repo_url = (
            self.gs_url.text().strip() or profile.gamma_setup_repo_url
        )
        profile.gamma_setup_repo_branch = self.gs_branch.text().strip() or "main"
        profile.stalker_gamma_repo_url = (
            self.sg_url.text().strip() or profile.stalker_gamma_repo_url
        )
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
        # Must mirror _save_or_create()'s own dispatch: whether the click
        # saves the profile the form was loaded from (rename included) or
        # creates a new one depends on _form_state, not on whether the
        # currently typed name happens to collide with some other profile.
        editing = bool(self._form_state) and any(
            p.profile_name == self._form_state for p in self.settings.profiles
        )
        if editing:
            self.save_button.setText(tr("Save Changes"))
            self.save_button.setToolTip(tr("Save changes to the selected profile."))
        else:
            self.save_button.setText(tr("Create profile"))
            self.save_button.setToolTip(tr("Create a new profile and activate it."))
        self._update_busy_state()

    def _update_busy_state(self) -> None:
        busy = self.window.install_busy
        for button in (self.new_button, self.active_button, self.delete_button, self.save_button):
            button.setEnabled(
                not busy and (button is self.new_button or self.profile_list.count() > 0)
            )
        for edit in (
            self.name_edit, self.anomaly_edit, self.gamma_edit, self.cache_edit,
            self.mo2_edit, self.threads_spin, self.modpack_edit, self.modlist_edit,
            self.gs_url, self.gs_branch, self.sg_url, self.sg_branch,
            self.glf_url, self.glf_branch, self.tg_url, self.tg_branch,
        ):
            edit.setEnabled(not busy)
        for button in self._browse_buttons:
            button.setEnabled(not busy)
        self.profile_list.setEnabled(not busy)

    def on_busy_changed(self, _busy: bool) -> None:
        """Disable profile edits while an install-affecting task is running."""
        self._update_busy_state()

    def _busy_guard(self) -> bool:
        if not self.window.install_busy:
            return False
        self._update_busy_state()
        return True

    def _save_or_create(self) -> None:
        if self._busy_guard():
            return
        name = self.name_edit.text().strip()
        # _form_state is the name of the profile the form was loaded from
        # (empty for "New"). Editing that profile - including renaming it -
        # must update that same entry in place instead of creating a
        # separate profile and leaving the original orphaned.
        editing_existing = bool(self._form_state) and any(
            p.profile_name == self._form_state for p in self.settings.profiles
        )
        # Renaming onto a *different* existing profile (or New keeping a name
        # that already exists) would silently overwrite that profile's data.
        collision = any(
            p.profile_name == name and p.profile_name != self._form_state
            for p in self.settings.profiles
        )
        if collision:
            QMessageBox.warning(
                self,
                tr("Name In Use"),
                tr("A profile named '{name}' already exists. Choose a different name.", name=name),
            )
            return
        if editing_existing:
            self._save_profile()
        else:
            self._create_profile()

    # ----- actions -----
    def _browse(self, edit: QLineEdit) -> None:
        if self._busy_guard():
            return
        path = QFileDialog.getExistingDirectory(self, "Select a folder", edit.text())
        if path:
            edit.setText(path)

    def _new_profile(self) -> None:
        if self._busy_guard():
            return
        # Preserve the paths already on screen: creating a new profile is
        # commonly done to add another MO2 profile on top of an existing
        # install (see seed_new_mo2_profile()), so resetting them to
        # CliProfile()'s placeholder defaults would make the shared install
        # paths the user just had visible seem to "disappear".
        anomaly = self.anomaly_edit.text()
        gamma = self.gamma_edit.text()
        cache = self.cache_edit.text()
        self._form_state = ""
        self._load_form(CliProfile())
        self.anomaly_edit.setText(anomaly)
        self.gamma_edit.setText(gamma)
        self.cache_edit.setText(cache)
        # An empty name field: "New" must never prefill a default name that
        # could collide with an existing profile.
        self.name_edit.clear()
        self._update_save_button()
        self.name_edit.setFocus()

    def _create_profile(self) -> None:
        profile = self._form_values()
        if not profile.profile_name:
            QMessageBox.warning(self, tr("Missing Name"), tr("A profile name is required."))
            return
        if not (profile.anomaly and profile.gamma and profile.cache):
            QMessageBox.warning(
                self,
                tr("Missing folders"),
                tr("Anomaly, GAMMA, and cache folders are required."),
            )
            return
        if self._task is not None:
            return
        if self._busy_guard():
            return
        args = [
            "create",
            "--anomaly",
            profile.anomaly,
            "--gamma",
            profile.gamma,
            "--cache",
            profile.cache,
            "--name",
            profile.profile_name,
            "--mo2-profile",
            profile.mo2_profile,
            "--mod-pack-maker-url",
            profile.mod_pack_maker_url,
            "--mod-list-url",
            profile.mod_list_url,
            "--download-threads",
            str(profile.download_threads),
            "--gamma-setup-repo-url",
            profile.gamma_setup_repo_url,
            "--gamma-setup-repo-branch",
            profile.gamma_setup_repo_branch,
            "--stalker-gamma-repo-url",
            profile.stalker_gamma_repo_url,
            "--stalker-gamma-repo-branch",
            profile.stalker_gamma_repo_branch,
            "--gamma-large-files-repo-url",
            profile.gamma_large_files_repo_url,
            "--gamma-large-files-repo-branch",
            profile.gamma_large_files_repo_branch,
            "--teivaz-anomaly-gunslinger-repo-url",
            profile.teivaz_anomaly_gunslinger_repo_url,
            "--teivaz-anomaly-gunslinger-repo-branch",
            profile.teivaz_anomaly_gunslinger_repo_branch,
        ]
        self._set_buttons_enabled(False)
        self._task = BackgroundTask(run_config_command, args, timeout=300, parent=self)
        self._task.result.connect(lambda res: self._on_create_done(profile, *res))
        self._task.error.connect(self._on_task_error)
        self._task.start()

    def _on_create_done(self, profile: CliProfile, rc: int, out: str, err: str) -> None:
        self._task = None
        self._set_buttons_enabled(True)
        if not cli_ok(rc, out, err):
            QMessageBox.warning(
                self,
                tr("Create Failed"),
                (out + "\n" + err).strip() or "config create failed",
            )
            return
        self.window.refresh_settings()
        if not any(
            p.profile_name == profile.profile_name
            for p in self.window.settings.profiles
        ):
            QMessageBox.warning(
                self, tr("Create Failed"), tr("The CLI did not create the profile.")
            )
            return
        try:
            seed_new_mo2_profile(profile.gamma, profile.mo2_profile)
        except OSError:
            # Best-effort: the profile itself was created successfully above,
            # so a failure here (e.g. a read-only gamma folder) must not be
            # reported as the create having failed.
            pass
        self.refresh()
        QMessageBox.information(
            self, tr("Created"), tr("Profile '{profile_name}' created and activated.", profile_name=profile.profile_name)
        )

    def _save_profile(self) -> None:
        if self._task is not None:
            return
        profile = self._form_values()
        if not profile.profile_name:
            QMessageBox.warning(self, tr("Missing Name"), tr("A profile name is required."))
            return
        if not (profile.anomaly and profile.gamma and profile.cache):
            QMessageBox.warning(
                self,
                tr("Missing folders"),
                tr("Anomaly, GAMMA, and cache folders are required."),
            )
            return
        active = self.settings.active_profile
        # Match on the name the form was loaded from, not the (possibly
        # just-renamed) new name, so a rename replaces the original entry
        # instead of leaving it behind as an orphaned duplicate.
        existing = next(
            (
                p
                for p in self.settings.profiles
                if p.profile_name == self._form_state
            ),
            None,
        )
        # Build the new list without mutating self.settings.profiles yet.
        profiles = [p for p in self.settings.profiles if p is not existing]
        if existing is not None:
            profile.extra = dict(existing.extra)
        profile.active = bool(
            active is None
            or (existing is not None and existing.profile_name == active.profile_name)
        )
        profiles.append(profile)
        if self._busy_guard():
            return
        original_profiles = self.settings.profiles[:]
        self.settings.profiles = profiles
        try:
            self.settings.save()
        except OSError as exc:
            self.settings.profiles = original_profiles
            QMessageBox.warning(
                self, tr("Save Failed"), tr("Could not write settings.json:\n{exc}", exc=exc)
            )
            return
        self._form_state = profile.profile_name
        self.window.refresh_settings()
        self.refresh()
        QMessageBox.information(
            self, tr("Saved"), tr("Profile '{profile_name}' saved.", profile_name=profile.profile_name)
        )

    def _set_buttons_enabled(self, enabled: bool) -> None:
        if self.window.install_busy:
            self._update_busy_state()
            return
        self.new_button.setEnabled(enabled)
        self.active_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        self.save_button.setEnabled(enabled)

    def _on_task_error(self, msg: str) -> None:
        self._task = None
        self._set_buttons_enabled(True)
        QMessageBox.warning(self, tr("Error"), msg)

    def _selected_profile_name(self) -> str | None:
        """Name of the highlighted profile, or None when the list is empty."""
        item = self.profile_list.currentItem()
        if item is None:
            QMessageBox.information(
                self, tr("No Selection"), tr("Select a profile in the list first.")
            )
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _warn_if_active_profile_running(self, name: str, message: str) -> bool:
        """Ask to continue when MO2/the game is up and ``name`` is active.

        COMMANDER cannot tell which profile a running MO2 actually belongs
        to, so this only fires for the one case it *can* reason about: the
        selected profile is the current active one and MO2 is running.
        Returns True if the caller should proceed.
        """
        active = self.settings.active_profile
        if active is None or active.profile_name != name or not mo2_running():
            return True
        answer = QMessageBox.question(
            self,
            tr("Game Running"),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _set_active(self) -> None:
        if self._busy_guard():
            return
        name = self._selected_profile_name()
        if name is None:
            return
        if self.settings.active_profile is not None and mo2_running():
            active_name = self.settings.active_profile.profile_name
            if name != active_name:
                answer = QMessageBox.question(
                    self,
                    tr("Game Running"),
                    tr("Mod Organizer / the game appears to be running under the current active profile ('{active_name}').\n\nSwitching the active profile now will not stop it, but COMMANDER's other pages will stop reflecting its state.\n\nSwitch anyway?", active_name=active_name),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
        if self._task is not None:
            return
        if self._busy_guard():
            return
        self._set_buttons_enabled(False)
        self._task = BackgroundTask(
            run_config_command, ["use", name], timeout=300, parent=self
        )
        self._task.result.connect(lambda res: self._on_set_active_done(name, *res))
        self._task.error.connect(self._on_task_error)
        self._task.start()

    def _on_set_active_done(self, name: str, rc: int, out: str, err: str) -> None:
        self._task = None
        self._set_buttons_enabled(True)
        if not cli_ok(rc, out, err):
            QMessageBox.warning(
                self, tr("Failed"), (out + "\n" + err).strip() or "config use failed"
            )
            return
        self.window.refresh_settings()
        active = self.window.settings.active_profile
        if active is None or active.profile_name != name:
            QMessageBox.warning(
                self, tr("Failed"), tr("Profile '{name}' could not be activated.", name=name)
            )
            return
        self.refresh()
        QMessageBox.information(self, tr("Activated"), tr("Profile '{name}' is now active.", name=name))

    def _delete_profile(self) -> None:
        if self._busy_guard():
            return
        name = self._selected_profile_name()
        if name is None:
            return
        answer = QMessageBox.question(
            self,
            tr("Delete Profile"),
            tr("Delete profile '{name}'?", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not self._warn_if_active_profile_running(
            name,
            "Mod Organizer / the game appears to be running under this "
            "profile.\n\n"
            "Deleting it now will not stop it, but COMMANDER will no "
            "longer have a profile to show its state under.\n\n"
            "Delete anyway?",
        ):
            return
        if self._task is not None:
            return
        if self._busy_guard():
            return
        self._set_buttons_enabled(False)
        self._task = BackgroundTask(
            run_config_command, ["delete", name], timeout=300, parent=self
        )
        self._task.result.connect(lambda res: self._on_delete_done(name, *res))
        self._task.error.connect(self._on_task_error)
        self._task.start()

    def _on_delete_done(self, name: str, rc: int, out: str, err: str) -> None:
        self._task = None
        self._set_buttons_enabled(True)
        if not cli_ok(rc, out, err):
            QMessageBox.warning(
                self, tr("Failed"), (out + "\n" + err).strip() or "config delete failed"
            )
            return
        self.window.refresh_settings()
        if any(p.profile_name == name for p in self.window.settings.profiles):
            QMessageBox.warning(
                self, tr("Failed"), tr("Profile '{name}' could not be deleted.", name=name)
            )
            return
        self.refresh()
