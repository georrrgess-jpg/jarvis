"""A small Windows helper that stays running and answers JSON questions JARVIS can't ask from Python alone.

* ``media.sessions`` / ``media.control``: Windows' own media sessions (the Global System Media Transport
  Controls: what the volume flyout shows). Chrome and Edge (YouTube, YouTube Music, any site using the
  Media Session API), Spotify, VLC, Windows Media Player and others register there, with the title, the
  artist, whether it is playing or paused, the position, and which buttons work. Pausing through it pauses
  *that* player, and reading it back afterwards proves it worked.
* ``audio.sessions`` / ``audio.set``: each app's own volume and mute in the Windows mixer (Core Audio), so
  "turn the music down" lowers the music and not JARVIS, and music can be lowered while JARVIS speaks.
* ``browser.url``: the address shown in a browser window, read through Windows accessibility (UI
  Automation), so JARVIS can tell a Google sign-in page from the page it meant to open.

One PowerShell process runs the lot (no installs, nothing leaves the PC); each part reports its own
availability, so a missing piece (e.g. no sound device) never takes the others down.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

log = logging.getLogger("jarvis.winhelper")

_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$caps = @{ media = $false; audio = $false; uia = $false }
$errs = @{}

# ---------------------------------------------------------------- media sessions (WinRT)
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  function Await($op, [Type]$type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $null = $t.Wait(5000); if (-not $t.IsCompleted) { throw 'timed out' }; $t.Result }
  $mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]
  $script:mgr = Await ($mgrType::RequestAsync()) ($mgrType)
  $caps.media = $true
} catch { $errs.media = $_.Exception.Message }

function Session-Info($s, $i, $currentId) {
  $props = $null
  try { $props = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]) } catch {}
  $info = $s.GetPlaybackInfo()
  $c = $info.Controls
  $tl = $null
  try { $tl = $s.GetTimelineProperties() } catch {}
  $pos = $null; $end = $null; $age = $null
  if ($tl) {
    $pos = [math]::Round($tl.Position.TotalSeconds, 1); $end = [math]::Round($tl.EndTime.TotalSeconds, 1)
    try { $age = [math]::Round(([DateTimeOffset]::Now - $tl.LastUpdatedTime).TotalSeconds, 1) } catch {}
  }
  @{ index = $i; app = $s.SourceAppUserModelId; current = ($s.SourceAppUserModelId -eq $currentId);
     title = $(if ($props) { $props.Title } else { '' }); artist = $(if ($props) { $props.Artist } else { '' });
     album = $(if ($props) { $props.AlbumTitle } else { '' }); status = $info.PlaybackStatus.ToString();
     position = $pos; duration = $end; updated_ago = $age;
     can = @{ play = $c.IsPlayEnabled; pause = $c.IsPauseEnabled; toggle = $c.IsPlayPauseToggleEnabled; next = $c.IsNextEnabled;
              previous = $c.IsPreviousEnabled; stop = $c.IsStopEnabled; seek = $c.IsPlaybackPositionEnabled } }
}

function Find-Session($req) {
  $all = @($script:mgr.GetSessions())
  $match = @($all | Where-Object { $_.SourceAppUserModelId -eq $req.app })
  if ($req.title) {
    foreach ($s in $match) {
      try { $p = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]); if ($p.Title -eq $req.title) { return $s } } catch {}
    }
  }
  if ($match.Count -gt 0) {
    if ($null -ne $req.index -and $req.index -lt $all.Count -and $all[$req.index].SourceAppUserModelId -eq $req.app) { return $all[$req.index] }
    return $match[0]
  }
  return $null
}

# ---------------------------------------------------------------- app volumes (Core Audio)
$audioCode = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace JarvisAudio {
  [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorCo {}
  [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDeviceEnumerator { int EnumAudioEndpoints(int a, int b, out IntPtr c); [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device); }
  [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDevice { [PreserveSig] int Activate(ref Guid iid, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o); }
  [Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionManager2 { int GetAudioSessionControl(IntPtr a, int b, out IntPtr c); int GetSimpleAudioVolume(IntPtr a, int b, out IntPtr c);
    [PreserveSig] int GetSessionEnumerator(out IAudioSessionEnumerator e); }
  [Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionEnumerator { [PreserveSig] int GetCount(out int n); [PreserveSig] int GetSession(int i, out IAudioSessionControl2 s); }
  [Guid("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionControl2 {
    [PreserveSig] int GetState(out int state);
    [PreserveSig] int GetDisplayName([MarshalAs(UnmanagedType.LPWStr)] out string name);
    int SetDisplayName(string a, ref Guid b); int GetIconPath(out IntPtr a); int SetIconPath(string a, ref Guid b);
    int GetGroupingParam(out Guid a); int SetGroupingParam(ref Guid a, ref Guid b);
    int RegisterAudioSessionNotification(IntPtr a); int UnregisterAudioSessionNotification(IntPtr a);
    int GetSessionIdentifier(out IntPtr a); int GetSessionInstanceIdentifier(out IntPtr a);
    [PreserveSig] int GetProcessId(out uint pid); [PreserveSig] int IsSystemSoundsSession(); int SetDuckingPreference(bool a);
  }
  [Guid("87CE5498-68D6-44E5-9215-6DA47EF883D8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface ISimpleAudioVolume { [PreserveSig] int SetMasterVolume(float level, ref Guid ctx); [PreserveSig] int GetMasterVolume(out float level);
    [PreserveSig] int SetMute(bool mute, ref Guid ctx); [PreserveSig] int GetMute(out bool mute); }
  public class Session { public uint Pid; public int State; public float Volume; public bool Muted; public bool System; }
  public static class Mixer {
    static IAudioSessionManager2 Manager() {
      var en = (IMMDeviceEnumerator)(new MMDeviceEnumeratorCo());
      IMMDevice dev; int hr = en.GetDefaultAudioEndpoint(0, 1, out dev);
      if (hr != 0 || dev == null) throw new Exception("no sound output device");
      Guid iid = typeof(IAudioSessionManager2).GUID; object o;
      Marshal.ThrowExceptionForHR(dev.Activate(ref iid, 23, IntPtr.Zero, out o));
      return (IAudioSessionManager2)o;
    }
    static List<IAudioSessionControl2> Controls() {
      var list = new List<IAudioSessionControl2>(); IAudioSessionEnumerator e;
      Marshal.ThrowExceptionForHR(Manager().GetSessionEnumerator(out e)); int n; e.GetCount(out n);
      for (int i = 0; i < n; i++) { IAudioSessionControl2 c; if (e.GetSession(i, out c) == 0 && c != null) list.Add(c); }
      return list;
    }
    public static List<Session> Sessions() {
      var result = new List<Session>();
      foreach (var c in Controls()) {
        var s = new Session(); c.GetProcessId(out s.Pid); c.GetState(out s.State); s.System = c.IsSystemSoundsSession() == 0;
        var v = (ISimpleAudioVolume)c; v.GetMasterVolume(out s.Volume); v.GetMute(out s.Muted); result.Add(s);
      }
      return result;
    }
    public static int Set(uint pid, float volume, int mute) {
      int changed = 0; Guid g = Guid.Empty;
      foreach (var c in Controls()) {
        uint p; c.GetProcessId(out p); if (p != pid) continue;
        var v = (ISimpleAudioVolume)c;
        if (volume >= 0) v.SetMasterVolume(Math.Max(0f, Math.Min(1f, volume)), ref g);
        if (mute >= 0) v.SetMute(mute == 1, ref g);
        changed++;
      }
      return changed;
    }
  }
}
'@
try { Add-Type -TypeDefinition $audioCode -Language CSharp; $null = [JarvisAudio.Mixer]::Sessions(); $caps.audio = $true } catch { $errs.audio = $_.Exception.Message }

# ---------------------------------------------------------------- browser address bar (UI Automation)
try {
  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes
  $caps.uia = $true
} catch { $errs.uia = $_.Exception.Message }

function Browser-Url([long]$hwnd) {
  $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$hwnd)
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
  $edit = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
  if ($null -eq $edit) { return @{ ok = $false; error = 'no address bar' } }
  $vp = $edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
  @{ ok = $true; url = $vp.Current.Value; name = $edit.Current.Name }
}

[Console]::Out.WriteLine((@{ ready = $true; caps = $caps; errors = $errs } | ConvertTo-Json -Compress))
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line -or $line -eq 'exit') { break }
  try {
    $req = $line | ConvertFrom-Json
    switch ($req.cmd) {
      'ping' { $out = @{ ok = $true; caps = $caps } }
      'media.sessions' {
        if (-not $caps.media) { throw ('media sessions unavailable: ' + $errs.media) }
        $cur = $script:mgr.GetCurrentSession(); $curId = $(if ($cur) { $cur.SourceAppUserModelId } else { '' })
        $all = @($script:mgr.GetSessions()); $i = 0
        $list = @(foreach ($s in $all) { Session-Info $s $i $curId; $i++ })
        $out = @{ ok = $true; sessions = $list }
      }
      'media.control' {
        if (-not $caps.media) { throw ('media sessions unavailable: ' + $errs.media) }
        $s = Find-Session $req
        if ($null -eq $s) { $out = @{ ok = $false; error = 'that player has gone' } }
        else {
          $op = switch ($req.action) {
            'play' { $s.TryPlayAsync() } 'pause' { $s.TryPauseAsync() } 'toggle' { $s.TryTogglePlayPauseAsync() }
            'next' { $s.TrySkipNextAsync() } 'previous' { $s.TrySkipPreviousAsync() } 'stop' { $s.TryStopAsync() }
            'seek' { $s.TryChangePlaybackPositionAsync([long]([double]$req.position * 10000000)) }
            default { throw ('unknown action ' + $req.action) } }
          $out = @{ ok = [bool](Await $op ([bool])) }
        }
      }
      'audio.sessions' {
        if (-not $caps.audio) { throw ('app volumes unavailable: ' + $errs.audio) }
        $list = @(foreach ($s in [JarvisAudio.Mixer]::Sessions()) {
          $name = ''; try { $name = (Get-Process -Id $s.Pid -ErrorAction Stop).ProcessName } catch {}
          @{ pid = $s.Pid; name = $name; state = $s.State; volume = [math]::Round($s.Volume, 3); muted = $s.Muted; system = $s.System } })
        $out = @{ ok = $true; sessions = $list }
      }
      'audio.set' {
        if (-not $caps.audio) { throw ('app volumes unavailable: ' + $errs.audio) }
        $vol = $(if ($null -ne $req.volume) { [float]$req.volume } else { -1 })
        $mute = $(if ($null -ne $req.muted) { [int][bool]$req.muted } else { -1 })
        $out = @{ ok = $true; changed = [JarvisAudio.Mixer]::Set([uint32]$req.pid, $vol, $mute) }
      }
      'browser.url' {
        if (-not $caps.uia) { throw ('accessibility unavailable: ' + $errs.uia) }
        $out = Browser-Url ([long]$req.hwnd)
      }
      default { $out = @{ ok = $false; error = ('unknown command ' + $req.cmd) } }
    }
  } catch { $out = @{ ok = $false; error = $_.Exception.Message } }
  [Console]::Out.WriteLine(($out | ConvertTo-Json -Depth 6 -Compress))
}
"""


class HelperUnavailable(Exception):
    pass


class WinHelper:
    """The running helper. Thread-safe; starts on first use; restarts itself (at most once a minute) if it dies."""

    RETRY_AFTER = 60.0

    def __init__(self, script: str = _PS_SCRIPT, platform: str | None = None) -> None:
        self._script = script
        self._platform = platform or sys.platform
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.caps: dict = {}
        self.errors: dict = {}
        self.error: str | None = None
        self._failed_at = 0.0

    # -- lifecycle
    def _start(self) -> None:
        folder = Path(tempfile.gettempdir()) / "jarvis-helper"
        folder.mkdir(exist_ok=True)
        path = folder / "helper.ps1"
        path.write_text(self._script, encoding="utf-8-sig")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            bufsize=1, creationflags=flags)
        hello = self._readline(40)
        info = json.loads(hello) if hello else {}
        if not info.get("ready"):
            self.stop()
            raise HelperUnavailable("the Windows helper didn't start")
        self.caps = dict(info.get("caps") or {})
        self.errors = dict(info.get("errors") or {})
        log.info("Windows helper ready: %s %s", self.caps, self.errors or "")

    def _readline(self, timeout: float) -> str:
        proc = self._proc
        result: list[str] = []
        reader = threading.Thread(target=lambda: result.append(proc.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout)
        if not result:
            self.stop()
            raise HelperUnavailable("the Windows helper stopped responding")
        return result[0].strip()

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            for action in (lambda: (proc.stdin.write("exit\n"), proc.stdin.flush()), proc.kill):
                try:
                    action()
                except Exception:
                    pass

    def available(self) -> bool:
        if self._platform != "win32":
            self.error = "only on Windows"
            return False
        with self._lock:
            return self._ensure()

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        if self._failed_at and time.monotonic() - self._failed_at < self.RETRY_AFTER:
            return False
        try:
            self._start()
            self.error, self._failed_at = None, 0.0
            return True
        except (HelperUnavailable, OSError, ValueError) as exc:
            self.error, self._failed_at = str(exc), time.monotonic()
            log.info("Windows helper unavailable: %s", exc)
            return False

    def has(self, cap: str) -> bool:
        return self.available() and bool(self.caps.get(cap))

    # -- requests
    def call(self, cmd: str, timeout: float = 8.0, **args) -> dict:
        if self._platform != "win32":
            raise HelperUnavailable("only on Windows")
        with self._lock:
            if not self._ensure():
                raise HelperUnavailable(self.error or "the Windows helper is unavailable")
            self._proc.stdin.write(json.dumps({"cmd": cmd, **args}) + "\n")
            self._proc.stdin.flush()
            reply = self._readline(timeout)
        try:
            data = json.loads(reply or "{}")
        except ValueError as exc:
            raise HelperUnavailable(f"unexpected reply from the Windows helper: {reply[:120]}") from exc
        return data if isinstance(data, dict) else {"ok": False, "error": "unexpected reply"}

    def status(self) -> dict:
        return {"running": self._proc is not None and self._proc.poll() is None, "caps": dict(self.caps),
                "errors": dict(self.errors), "error": self.error}
