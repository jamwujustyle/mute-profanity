import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Dialog from 'resource:///org/gnome/shell/ui/dialog.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as ModalDialog from 'resource:///org/gnome/shell/ui/modalDialog.js';

const OBJECT_PATH = '/org/gnome/Shell/Extensions/MuteProfanity';
const INTERFACE_XML = `
<node>
  <interface name="org.gnome.Shell.Extensions.MuteProfanity">
    <method name="Show">
      <arg type="u" name="seconds" direction="in"/>
      <arg type="b" name="shown" direction="out"/>
    </method>
    <method name="Hide"/>
  </interface>
</node>`;

const MAX_SECONDS = 60;

const CooldownDialog = GObject.registerClass(
class CooldownDialog extends ModalDialog.ModalDialog {
    _init(pause) {
        // GNOME's ModalDialog binds its outer actor and event-inhibiting
        // lightbox to the entire stage, including secondary monitors. open()
        // succeeds only when Main.pushModal acquired the full keyboard/pointer
        // seat grab. The card itself follows GNOME's current monitor policy.
        super._init({
            shellReactive: false,
            actionMode: Shell.ActionMode.SYSTEM_MODAL,
            shouldFadeIn: false,
            shouldFadeOut: false,
            destroyOnClose: true,
        });
        this._timer = 0;
        this._deadline = 0;
        this._maximumDeadline = GLib.get_monotonic_time() + MAX_SECONDS * 1_000_000;
        this._pause = pause;
        this.contentLayout.add_child(new Dialog.MessageDialogContent({
            title: 'Watch your language.',
            description: 'Profanity blocked. Take a breath and rephrase.',
        }));
        this._countdown = new St.Label({
            style_class: 'message-dialog-description',
            x_align: Clutter.ActorAlign.CENTER,
        });
        this.contentLayout.add_child(this._countdown);
        const hint = new St.Label({
            text: 'Ctrl+Shift+F12: dismiss and pause the filter for 5 minutes',
            style_class: 'message-dialog-description',
        });
        hint.clutter_text.line_wrap = true;
        this.contentLayout.add_child(hint);
        this.connect('captured-event', (_actor, event) => {
            if (event.type() === Clutter.EventType.KEY_PRESS) {
                const required = Clutter.ModifierType.CONTROL_MASK | Clutter.ModifierType.SHIFT_MASK;
                if (event.get_key_symbol() === Clutter.KEY_F12 &&
                    (event.get_state() & required) === required)
                    this._pause();
            }
            // The modal deliberately has no close/submit actions. Only the
            // countdown and the emergency shortcut release desktop input.
            return Clutter.EVENT_STOP;
        });
        this.connect('destroy', () => {
            this._stopTimer();
            this.popModal();
            this._pause = null;
        });
    }

    showFor(seconds) {
        this._deadline = Math.min(this._maximumDeadline,
            Math.max(this._deadline, GLib.get_monotonic_time() + seconds * 1_000_000));
        if (!this.open())
            return false;
        this._updateCountdown();
        if (!this._timer) {
            this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 250, () => {
                try {
                    if (this._updateCountdown())
                        return GLib.SOURCE_CONTINUE;
                } catch (error) {
                    logError(error, 'Mute cooldown was closed after a dialog error');
                }
                this._timer = 0;
                try {
                    this.dismiss();
                } catch (error) {
                    logError(error, 'Mute cooldown cleanup failed');
                }
                return GLib.SOURCE_REMOVE;
            });
        }
        return true;
    }

    _updateCountdown() {
        const remaining = Math.max(0, Math.ceil((this._deadline - GLib.get_monotonic_time()) / 1_000_000));
        this._countdown.text = `Continue in ${remaining} ${remaining === 1 ? 'second' : 'seconds'}.`;
        return remaining > 0;
    }

    _stopTimer() {
        if (this._timer) {
            GLib.Source.remove(this._timer);
            this._timer = 0;
        }
    }

    dismiss() {
        this._stopTimer();
        // Always release the seat before actor destruction, including when
        // open() could not acquire it or the extension is disabled mid-pause.
        try {
            this.popModal();
        } finally {
            this.destroy();
        }
    }
});

export default class MuteProfanityExtension extends Extension {
    enable() {
        this._dialog = null;
        this._dbus = null;
        this._searchEntryText = null;
        this._searchEntryWasSensitive = false;
        const bridge = this.dir.get_child('bridge.json');
        const size = bridge.query_info('standard::size', Gio.FileQueryInfoFlags.NONE, null).get_size();
        if (size > 4096)
            throw new Error('Mute bridge configuration is too large');
        const [, bytes] = bridge.load_contents(null);
        const config = JSON.parse(new TextDecoder().decode(bytes));
        if (typeof config.entry_path !== 'string' || !GLib.path_is_absolute(config.entry_path) ||
            !Gio.File.new_for_path(config.entry_path).query_exists(null))
            throw new Error('Mute requires an installed Python entry point');
        this._entryPath = config.entry_path;
        try {
            this._markSearchEntry();
            this._dbus = Gio.DBusExportedObject.wrapJSObject(INTERFACE_XML, this);
            this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        } catch (error) {
            try {
                this._dbus?.unexport();
            } finally {
                this._dbus = null;
                this._entryPath = null;
                this._restoreSearchEntryHints();
            }
            throw error;
        }
    }

    _markSearchEntry() {
        // Shell search needs each key immediately to update application
        // results. Mark this one entry private so IBus bypasses composition;
        // its visible text and every other Shell/application entry are intact.
        const entry = Main.overview.searchEntry.clutter_text;
        const sensitive = Clutter.InputContentHintFlags.SENSITIVE_DATA;
        const hints = entry.get_input_hints();
        this._searchEntryText = entry;
        this._searchEntryWasSensitive = Boolean(hints & sensitive);
        // Use the typed accessors: Mutter 46's flags GObject property is
        // marshalled as an enum, while these methods preserve the flags.
        entry.set_input_hints(hints | sensitive);
    }

    _restoreSearchEntryHints() {
        const entry = this._searchEntryText;
        const wasSensitive = this._searchEntryWasSensitive;
        this._searchEntryText = null;
        this._searchEntryWasSensitive = false;
        if (entry) {
            const sensitive = Clutter.InputContentHintFlags.SENSITIVE_DATA;
            const hints = entry.get_input_hints();
            // Other extensions may have changed unrelated hints meanwhile.
            entry.set_input_hints(wasSensitive
                ? hints | sensitive
                : hints & ~sensitive);
        }
    }

    Show(seconds) {
        const duration = Math.max(1, Math.min(MAX_SECONDS, Math.ceil(seconds)));
        try {
            if (!this._dialog) {
                const dialog = new CooldownDialog(() => this._emergencyPause());
                this._dialog = dialog;
                dialog.connect('destroy', () => {
                    if (this._dialog === dialog)
                        this._dialog = null;
                });
            }
            if (this._dialog.showFor(duration))
                return true;
            this.Hide();
            return false;
        } catch (error) {
            this.Hide();
            throw error;
        }
    }

    Hide() {
        const dialog = this._dialog;
        this._dialog = null;
        dialog?.dismiss();
    }

    _emergencyPause() {
        const entry = this._entryPath;
        this.Hide();
        try {
            // Pass argv directly: installation paths are never shell code.
            const process = Gio.Subprocess.new(['/usr/bin/python3', entry, 'pause'],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
            process.wait_async(null, (source, result) => {
                try {
                    source.wait_finish(result);
                } catch (error) {
                    logError(error, 'Mute emergency pause could not finish');
                }
            });
        } catch (error) {
            logError(error, 'Mute emergency pause could not start');
        }
    }

    disable() {
        try {
            this.Hide();
        } finally {
            try {
                this._dbus?.unexport();
            } finally {
                this._dbus = null;
                this._entryPath = null;
                this._restoreSearchEntryHints();
            }
        }
    }
}
