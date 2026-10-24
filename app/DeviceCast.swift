// DeviceCast.swift - menu bar front end for sender/devicecast.py.
// Built by app/build.sh with swiftc; depends only on Cocoa.
import Cocoa

// The app does not bundle the scripts: it runs them from the project folder so that editing them
// does not change the app signature. If the app is moved, build.sh records the project path in Info.plist.
let bundleDir = (Bundle.main.bundlePath as NSString).deletingLastPathComponent
let projectDir: String = {
    if FileManager.default.fileExists(atPath: bundleDir + "/sender/devicecast.py") { return bundleDir }
    if let p = Bundle.main.object(forInfoDictionaryKey: "DeviceCastProjectDir") as? String,
       FileManager.default.fileExists(atPath: p + "/sender/devicecast.py") { return p }
    return bundleDir
}()
let dataDir = projectDir + "/data"
let logPath = dataDir + "/devicecast.log"
let configPath = dataDir + "/config.json"
/// Read by the server while casting: "video" switches the TV to H.264, "tiles" back to tiles.
let controlPath = dataDir + "/control"
let scriptPath = projectDir + "/sender/devicecast.py"

struct TV { let udn: String; let name: String; let ip: String; let model: String; let location: String }

/// Appends a line to the log shared with devicecast.py.
func menuLog(_ text: String) {
    let dir = (logPath as NSString).deletingLastPathComponent
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    if !FileManager.default.fileExists(atPath: logPath) {
        FileManager.default.createFile(atPath: logPath, contents: nil)
    }
    let f = DateFormatter(); f.dateFormat = "HH:mm:ss"
    if let fh = FileHandle(forWritingAtPath: logPath) {
        fh.seekToEndOfFile()
        fh.write("[\(f.string(from: Date()))] [menu] \(text)\n".data(using: .utf8)!)
        fh.closeFile()
    }
}
struct Screen { let index: Int; let name: String }

let qualities: [(label: String, args: [String])] = [
    ("720p (recommended)", ["--width", "1280", "--height", "720", "--bitrate", "5M"]),
    ("1080p", ["--width", "1920", "--height", "1080", "--bitrate", "8M"]),
    ("540p (slow network)", ["--width", "960", "--height", "540", "--bitrate", "3M"]),
]

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    let menu = NSMenu()
    var cast: Process?
    var stoppingOnPurpose = false
    var tvs: [TV] = []
    var screens: [Screen] = []
    var searching = false
    var statusText = "Starting…"
    var alertsShown = Set<String>()

    // Shared with devicecast.py.
    var config: [String: Any] = [:]
    var selectedUDN: String? { config["tv_udn"] as? String }
    var selectedScreen: String? { config["screen"] as? String }
    var qualityIndex: Int { config["quality"] as? Int ?? 0 }
    var latencyLow: Bool { (config["latency"] as? String ?? "low") == "low" }
    var autoQuality: Bool { config["auto_quality"] as? Bool ?? true }
    /// Video mode (H.264) for the current cast, chosen from the menu. Every cast starts in tiles mode.
    var videoMode = false

    // MARK: Lifecycle

    func applicationDidFinishLaunching(_: Notification) {
        if !FileManager.default.fileExists(atPath: scriptPath) {
            NSApp.activate(ignoringOtherApps: true)
            let a = NSAlert()
            a.messageText = "Device Cast scripts not found"
            a.informativeText = "Device Cast looks for sender/devicecast.py in \(projectDir). Keep the app in the project folder or rebuild it with app/build.sh."
            a.runModal()
            NSApp.terminate(nil)
        }
        menuLog("===== Device Cast started (\(Bundle.main.bundlePath)), script: \(scriptPath)")
        loadConfig()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.menu = menu
        updateIcon()
        rebuildMenu()
        refreshScreens()
        refreshTVs(autoStart: true)
    }

    /// The app was opened again (Spotlight, Launchpad, Dock) while already running.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if cast != nil {
            NSApp.activate(ignoringOtherApps: true)
            let a = NSAlert()
            a.messageText = "Casting to \(config["tv_name"] as? String ?? "the TV")"
            a.informativeText = "The Device Cast icon is in the menu bar (it may be hidden behind the » arrow when there are many icons)."
            a.addButton(withTitle: "Stop casting")
            a.addButton(withTitle: "Quit Device Cast")
            a.addButton(withTitle: "Continue")
            switch a.runModal() {
            case .alertFirstButtonReturn: stopCast()
            case .alertSecondButtonReturn: NSApp.terminate(nil)
            default: break
            }
        } else if !searching {
            if selectedUDN != nil || tvs.count == 1 { startCast() } else { refreshTVs(autoStart: true) }
        }
        return false
    }

    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let m = NSMenu()
        let st = NSMenuItem(title: statusText, action: nil, keyEquivalent: ""); st.isEnabled = false
        m.addItem(st)
        if cast == nil {
            let it = NSMenuItem(title: "Start casting", action: #selector(startClicked), keyEquivalent: ""); it.target = self
            m.addItem(it)
        } else {
            let it = NSMenuItem(title: "Stop casting", action: #selector(stopClicked), keyEquivalent: ""); it.target = self
            m.addItem(it)
        }
        let log = NSMenuItem(title: "Open log", action: #selector(openLog), keyEquivalent: ""); log.target = self
        m.addItem(log)
        return m
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard cast != nil else { return .terminateNow }
        stopCast { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }

    // MARK: Configuration

    func loadConfig() {
        if let data = FileManager.default.contents(atPath: configPath),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            config = obj
        }
        // Video mode is now chosen from the menu while casting; drop the old automatic setting.
        if config.removeValue(forKey: "auto_video") != nil { saveConfig() }
    }

    func saveConfig() {
        let dir = (configPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys]) {
            FileManager.default.createFile(atPath: configPath, contents: data)
        }
    }

    // MARK: Running devicecast.py

    func makeProcess(_ args: [String]) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["python3", scriptPath] + args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        return p
    }

    /// Runs the script in the background and returns its stdout.
    func runScript(_ args: [String], completion: @escaping (Data) -> Void) {
        DispatchQueue.global().async {
            let p = self.makeProcess(args)
            let pipe = Pipe()
            p.standardOutput = pipe
            if let fh = FileHandle(forWritingAtPath: logPath) { fh.seekToEndOfFile(); p.standardError = fh }
            var data = Data()
            do {
                try p.run()
                data = pipe.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
            } catch {}
            DispatchQueue.main.async { completion(data) }
        }
    }

    func refreshTVs(autoStart: Bool = false) {
        guard !searching else { return }
        searching = true
        setStatus("Searching for TVs…")
        menuLog("searching for TVs (python3 devicecast.py list --json)")
        runScript(["list", "--json", "--timeout", "3"]) { data in
            self.searching = false
            var found: [TV] = []
            if let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] {
                found = arr.map { TV(udn: $0["udn"] as? String ?? "", name: $0["name"] as? String ?? "?",
                                     ip: $0["ip"] as? String ?? "", model: $0["model"] as? String ?? "",
                                     location: $0["location"] as? String ?? "") }
                menuLog("found \(found.count) TV(s): " + found.map { "\($0.name) [\($0.ip)]" }.joined(separator: ", "))
            } else {
                menuLog("could not parse list --json output (\(data.count) bytes): "
                        + (String(data: data.prefix(300), encoding: .utf8) ?? ""))
            }
            self.tvs = found
            if self.cast == nil {
                if found.isEmpty, self.logTail(15).contains("Local Network permission denied") {
                    self.setStatus("Local Network permission missing")
                    self.showAlert("Device Cast cannot search for the TV", once: "localnet",
                                   info: "macOS has not granted the Local Network permission.\n\nOpen System Settings > Privacy & Security > Local Network and enable Device Cast. If it is not listed, quit and reopen the app: the permission prompt appears on the first attempt. Then choose TV > Search again.")
                    NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork")!)
                } else if found.isEmpty {
                    self.setStatus("No TV found. Is it on and on the same network?")
                } else if found.count == 1, self.selectedUDN == nil || !found.contains(where: { $0.udn == self.selectedUDN }) {
                    self.config["tv_udn"] = found[0].udn
                    self.config["tv_name"] = found[0].name
                    self.saveConfig()
                    self.setStatus("TV: \(found[0].name)")
                } else if found.contains(where: { $0.udn == self.selectedUDN }) {
                    self.setStatus("TV: \(self.config["tv_name"] as? String ?? "")")
                } else {
                    self.setStatus("Several TVs found: choose one in the TV menu")
                }
            }
            self.rebuildMenu()
            if autoStart, self.cast == nil, let udn = self.selectedUDN, found.contains(where: { $0.udn == udn }) {
                self.startCast()
            }
        }
    }

    func refreshScreens() {
        runScript(["devices", "--json"]) { data in
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let video = obj["video"] as? [[String: Any]] {
                self.screens = video.compactMap {
                    guard let n = $0["name"] as? String, n.lowercased().hasPrefix("capture screen") else { return nil }
                    return Screen(index: $0["index"] as? Int ?? 0, name: n)
                }
            }
            self.rebuildMenu()
        }
    }

    // MARK: Casting

    func castArgs() -> [String] {
        var args = ["cast"]
        if let udn = selectedUDN {
            args += ["--tv", udn]
            // The TV was just discovered: pass its location to skip a second search.
            if let tv = tvs.first(where: { $0.udn == udn }), !tv.location.isEmpty { args += ["--location", tv.location] }
        }
        args += qualities[min(qualityIndex, qualities.count - 1)].args
        if let s = selectedScreen { args += ["--screen", s] }
        args += ["--latency", latencyLow ? "low" : "normal"]
        if let extra = config["extra_args"] as? [String] { args += extra }
        return args
    }

    func startCast() {
        guard cast == nil else { return }
        guard selectedUDN != nil || tvs.count == 1 else {
            setStatus("Choose a TV in the TV menu first")
            return
        }
        let p = makeProcess(castArgs())
        let dir = (logPath as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        // Keep the log small: past 5 MB it becomes devicecast.log.1 (the previous one is dropped).
        if let size = (try? FileManager.default.attributesOfItem(atPath: logPath))?[.size] as? UInt64, size > 5_000_000 {
            try? FileManager.default.removeItem(atPath: logPath + ".1")
            try? FileManager.default.moveItem(atPath: logPath, toPath: logPath + ".1")
        }
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        if let fh = FileHandle(forWritingAtPath: logPath) {
            fh.seekToEndOfFile()
            fh.write("===== \(Date()) started from menu: \(castArgs().joined(separator: " "))\n".data(using: .utf8)!)
            p.standardOutput = fh
            p.standardError = fh
        }
        stoppingOnPurpose = false
        setVideoMode(false)
        p.terminationHandler = { proc in
            DispatchQueue.main.async { self.castEnded(status: proc.terminationStatus) }
        }
        do {
            try p.run()
            cast = p
            setStatus("Casting to \(config["tv_name"] as? String ?? "the TV")")
        } catch {
            showAlert("Could not start devicecast.py", info: "\(error)")
        }
        updateIcon()
        rebuildMenu()
    }

    func stopCast(completion: (() -> Void)? = nil) {
        guard let p = cast else { completion?(); return }
        stoppingOnPurpose = true
        setStatus("Stopping…")
        p.interrupt()  // SIGINT lets the script send Stop to the TV and shut down ffmpeg
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(6)
            while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
            if p.isRunning { p.terminate() }
            DispatchQueue.main.async { completion?() }
        }
    }

    func castEnded(status: Int32) {
        cast = nil
        updateIcon()
        let tail = logTail(12)
        if stoppingOnPurpose {
            setStatus("Casting stopped")
        } else if tail.contains("no data from capture") {
            setStatus("Screen Recording permission missing")
            showAlert("The TV received no picture", once: "screen",
                      info: "macOS has not granted Device Cast the Screen Recording permission.\n\nOpen System Settings > Privacy & Security > Screen Recording. If Device Cast is already enabled, turn its switch off and on again (the entry may still refer to an earlier build of the app); if it is missing, add it with +. Then quit and reopen Device Cast.")
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
        } else if tail.contains("TV stopped playback") {
            setStatus("TV stopped playback")
        } else if tail.contains("Device Cast was closed on the TV") {
            setStatus("Casting stopped on the TV")
        } else {
            let reason = tail.split(separator: "\n").last.map(String.init) ?? ""
            setStatus("Casting interrupted: " + reason.prefix(70))
        }
        rebuildMenu()
    }

    func logTail(_ n: Int) -> String {
        guard let s = try? String(contentsOfFile: logPath, encoding: .utf8) else { return "" }
        return s.split(separator: "\n").suffix(n).filter { !$0.contains("objc[") }.joined(separator: "\n")
    }

    // MARK: Interface

    func setStatus(_ t: String) { statusText = t; menuLog("status: " + t); rebuildMenu() }

    /// Template image from assets/MenuIcon*.png, so macOS tints it for light and dark menu bars.
    lazy var brandIcon: NSImage? = {
        guard let img = Bundle.main.image(forResource: "MenuIcon") else { return nil }
        img.size = NSSize(width: 27, height: 16)
        img.isTemplate = true
        return img
    }()

    func updateIcon() {
        if let img = brandIcon {
            statusItem.button?.image = img
            statusItem.button?.appearsDisabled = cast == nil   // dimmed when idle, full while casting
        } else if let img = NSImage(systemSymbolName: cast == nil ? "tv" : "tv.fill", accessibilityDescription: "Device Cast") {
            img.isTemplate = true
            statusItem.button?.image = img
        }
        statusItem.button?.toolTip = "Device Cast: " + statusText
    }

    func rebuildMenu() {
        menu.removeAllItems()
        let status = NSMenuItem(title: statusText, action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status)

        if cast == nil {
            let it = NSMenuItem(title: "Start casting", action: #selector(startClicked), keyEquivalent: "s")
            it.target = self
            it.isEnabled = !searching && (selectedUDN != nil || tvs.count == 1)
            menu.addItem(it)
        } else {
            let it = NSMenuItem(title: "Stop casting", action: #selector(stopClicked), keyEquivalent: "s")
            it.target = self
            menu.addItem(it)
            if latencyLow {
                let vm = NSMenuItem(title: "Video mode (smooth movies, slight delay)", action: #selector(videoModeClicked), keyEquivalent: "v")
                vm.target = self; vm.state = videoMode ? .on : .off
                menu.addItem(vm)
            }
        }
        menu.addItem(.separator())

        let tvMenu = NSMenu()
        if tvs.isEmpty {
            let none = NSMenuItem(title: searching ? "Searching…" : "No TV found", action: nil, keyEquivalent: "")
            none.isEnabled = false
            tvMenu.addItem(none)
        }
        for (i, tv) in tvs.enumerated() {
            let it = NSMenuItem(title: "\(tv.name)  (\(tv.model), \(tv.ip))", action: #selector(tvClicked(_:)), keyEquivalent: "")
            it.target = self; it.tag = i
            it.state = tv.udn == selectedUDN ? .on : .off
            tvMenu.addItem(it)
        }
        tvMenu.addItem(.separator())
        let search = NSMenuItem(title: "Search again", action: #selector(searchClicked), keyEquivalent: "r")
        search.target = self; search.isEnabled = !searching
        tvMenu.addItem(search)
        let tvItem = NSMenuItem(title: "TV", action: nil, keyEquivalent: "")
        tvItem.submenu = tvMenu
        menu.addItem(tvItem)

        let scrMenu = NSMenu()
        let auto = NSMenuItem(title: "Main display", action: #selector(screenClicked(_:)), keyEquivalent: "")
        auto.target = self; auto.tag = -1; auto.state = selectedScreen == nil ? .on : .off
        scrMenu.addItem(auto)
        for (i, s) in screens.enumerated() where i > 0 {
            let title = isExtendEntry(s.name) ? "TV as extended display (DeskPad)" : "Display \(i + 1)"
            let it = NSMenuItem(title: title, action: #selector(screenClicked(_:)), keyEquivalent: "")
            it.target = self; it.tag = i; it.state = selectedScreen == s.name ? .on : .off
            scrMenu.addItem(it)
        }
        scrMenu.addItem(.separator())
        let upd = NSMenuItem(title: "Refresh displays", action: #selector(screensRefreshClicked), keyEquivalent: "")
        upd.target = self
        scrMenu.addItem(upd)
        let scrItem = NSMenuItem(title: "Screen to cast", action: nil, keyEquivalent: "")
        scrItem.submenu = scrMenu
        menu.addItem(scrItem)

        let qMenu = NSMenu()
        for (i, q) in qualities.enumerated() {
            let it = NSMenuItem(title: q.label, action: #selector(qualityClicked(_:)), keyEquivalent: "")
            it.target = self; it.tag = i; it.state = i == qualityIndex ? .on : .off
            qMenu.addItem(it)
        }
        qMenu.addItem(.separator())
        let lat = NSMenuItem(title: "Low latency", action: #selector(latencyClicked), keyEquivalent: "")
        lat.target = self; lat.state = latencyLow ? .on : .off
        qMenu.addItem(lat)
        let aq = NSMenuItem(title: "Automatic quality (adapts to the network)", action: #selector(autoQualityClicked), keyEquivalent: "")
        aq.target = self; aq.state = autoQuality ? .on : .off; aq.isEnabled = latencyLow
        qMenu.addItem(aq)
        let qItem = NSMenuItem(title: "Quality", action: nil, keyEquivalent: "")
        qItem.submenu = qMenu
        menu.addItem(qItem)

        menu.addItem(.separator())
        let log = NSMenuItem(title: "Open log", action: #selector(openLog), keyEquivalent: "l")
        log.target = self
        menu.addItem(log)
        let quit = NSMenuItem(title: "Quit", action: #selector(quitClicked), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        updateIcon()
    }

    /// Matches deskpad.EXTEND_NAME.
    func isExtendEntry(_ name: String) -> Bool {
        name.lowercased().contains("extended display")
    }

    /// Modal warning; with `once` it is shown at most once per session.
    func showAlert(_ text: String, once key: String? = nil, info: String) {
        if let k = key {
            if alertsShown.contains(k) { return }
            alertsShown.insert(k)
        }
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = text
        a.informativeText = info
        a.alertStyle = .warning
        a.runModal()
    }

    /// Restarts an active cast after a TV, screen or quality change.
    func restartIfCasting() {
        guard cast != nil else { return }
        stopCast { DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.startCast() } }
    }

    // MARK: Actions

    @objc func startClicked() { startCast() }
    @objc func stopClicked() { stopCast() }
    @objc func searchClicked() { refreshTVs() }
    @objc func screensRefreshClicked() { refreshScreens() }
    @objc func openLog() { NSWorkspace.shared.open(URL(fileURLWithPath: logPath)) }
    @objc func quitClicked() { NSApp.terminate(nil) }

    @objc func tvClicked(_ sender: NSMenuItem) {
        let tv = tvs[sender.tag]
        config["tv_udn"] = tv.udn
        config["tv_name"] = tv.name
        saveConfig()
        setStatus("TV: \(tv.name)")
        restartIfCasting()
    }

    @objc func screenClicked(_ sender: NSMenuItem) {
        if sender.tag < 0 { config.removeValue(forKey: "screen") } else { config["screen"] = screens[sender.tag].name }
        saveConfig(); rebuildMenu(); restartIfCasting()
    }

    @objc func qualityClicked(_ sender: NSMenuItem) {
        config["quality"] = sender.tag
        saveConfig(); rebuildMenu(); restartIfCasting()
    }

    @objc func autoQualityClicked() {
        config["auto_quality"] = !autoQuality
        saveConfig(); rebuildMenu(); restartIfCasting()
    }

    @objc func videoModeClicked() {
        setVideoMode(!videoMode)
        rebuildMenu()
    }

    /// Takes effect on the running cast within a fraction of a second, without restarting it.
    func setVideoMode(_ on: Bool) {
        videoMode = on
        try? FileManager.default.createDirectory(atPath: dataDir, withIntermediateDirectories: true)
        try? (on ? "video\n" : "tiles\n").write(toFile: controlPath, atomically: true, encoding: .utf8)
    }

    @objc func latencyClicked() {
        config["latency"] = latencyLow ? "normal" : "low"
        saveConfig(); rebuildMenu(); restartIfCasting()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)   // Dock icon in addition to the menu bar item
app.run()
