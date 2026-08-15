"""Profiles page: create, edit, activate and delete CLI profiles."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..settings import CliProfile, run_config_command
from .common import make_card, section_label


class ProfilesPage(QWidget):
    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.settings = window.settings

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        top = QHBoxLayout()
        list_card, list_layout = make_card()
        top.addWidget(list_card, 1)
        form_card, form_layout = make_card()
        top.addWidget(form_card, 2)
        root.addLayout(top)

        # ----- profile list -----
        list_layout.addWidget(section_label("Profiles"))
        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(self._on_select)
        list_layout.addWidget(self.profile_list)

        btn_row = QHBoxLayout()
        self.new_button = QPushButton("New")
        self.new_button.clicked.connect(self._new_profile)
        self.active_button = QPushButton("Set Active")
        self.active_button.clicked.connect(self._set_active)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete_profile)
        for b in (self.new_button, self.active_button, self.delete_button):
            btn_row.addWidget(b)
        list_layout.addLayout(btn_row)

        # ----- form -----
        form_layout.addWidget(section_label("Profile Details"))
        self.form = QFormLayout()
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
            browse = QPushButton("...")
            browse.setMaximumWidth(40)
            browse.clicked.connect(lambda: self._browse(edit))
            row.addWidget(browse)
            return row

        self.form.addRow("Name", self.name_edit)
        self.form.addRow("Anomaly path", path_row(self.anomaly_edit))
        self.form.addRow("GAMMA path", path_row(self.gamma_edit))
        self.form.addRow("Cache path", path_row(self.cache_edit))
        self.form.addRow("MO2 profile", self.mo2_edit)
        self.form.addRow("Download threads", self.threads_spin)
        form_layout.addLayout(self.form)

        self.advanced_box = QGroupBox("Advanced (repos & URLs)")
        advanced_form = QFormLayout()
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
        for label, w in [
            ("ModPackMaker URL", self.modpack_edit),
            ("ModList URL", self.modlist_edit),
            ("gamma_setup URL", self.gs_url),
            ("gamma_setup branch", self.gs_branch),
            ("Stalker_GAMMA URL", self.sg_url),
            ("Stalker_GAMMA branch", self.sg_branch),
            ("gamma_large_files URL", self.glf_url),
            ("gamma_large_files branch", self.glf_branch),
            ("teivaz_anomaly_gunslinger URL", self.tg_url),
            ("teivaz_anomaly_gunslinger branch", self.tg_branch),
        ]:
            advanced_form.addRow(label, w)
        self.advanced_box.setLayout(advanced_form)
        form_layout.addWidget(self.advanced_box)

        form_btn_row = QHBoxLayout()
        self.save_button = QPushButton("Save Changes")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save_profile)
        self.create_button = QPushButton("Create Profile")
        self.create_button.setObjectName("primary")
        self.create_button.clicked.connect(self._create_profile)
        form_btn_row.addWidget(self.create_button)
        form_btn_row.addWidget(self.save_button)
        form_layout.addLayout(form_btn_row)
        self._form_state = ""

        self.refresh()

    # ----- list -----
    def refresh(self) -> None:
        self.window.refresh_settings()
        self.settings = self.window.settings
        previous = self._form_state
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for profile in self.settings.profiles:
            marker = "-> " if profile.active else "   "
            item = QListWidgetItem(f"{marker}{profile.profile_name}")
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_name)
            if profile.active:
                item.setForeground(Qt.GlobalColor.darkYellow)
            self.profile_list.addItem(item)
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

    # ----- actions -----
    def _browse(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select folder", edit.text())
        if path:
            edit.setText(path)

    def _new_profile(self) -> None:
        self._form_state = ""
        self._load_form(CliProfile())
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
        if rc != 0:
            QMessageBox.warning(
                self, "Create Failed", (out + "\n" + err).strip() or "config create failed"
            )
            return
        self.window.refresh_settings()
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
        if self._form_state:
            existing = next(
                (p for p in self.settings.profiles if p.profile_name == self._form_state),
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
            or (self._form_state and self._form_state == active.profile_name)
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
        if rc != 0:
            QMessageBox.warning(self, "Failed", (out + "\n" + err).strip() or "config use failed")
            return
        self.window.refresh_settings()
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
        if rc != 0:
            QMessageBox.warning(self, "Failed", (out + "\n" + err).strip() or "config delete failed")
            return
        self.window.refresh_settings()
        self.refresh()
