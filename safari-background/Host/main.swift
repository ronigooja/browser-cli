import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

func run(_ request: [String: Any], id: String) throws -> [String: Any] {
    func response(_ status: String) -> [String: Any] { ["v": IPC.version, "id": id, "status": status] }
    func blocked(_ reason: IPC.QualificationBlock) -> [String: Any] { reason.response(id: id) }
    guard IPC.integer(request["v"]) == IPC.version else { return response("protocol_mismatch") }
    guard IPC.id(request["id"]) != nil, let operation = request["operation"] as? String, IPC.operations.contains(operation),
          let payload = request["payload"] as? [String: Any], let expires = IPC.integer(request["expires"]) else { return response("invalid_request") }
    let diagnostic = operation == "qualify_window_diagnose"
    let qualification = operation == "qualify_window"
    var fields: Set<String> = ["v", "id", "operation", "payload", "expires"]
    if !diagnostic { fields.insert("profile") }
    let profile = (request["profile"] as? String).flatMap(UUID.init(uuidString:))
    guard Set(request.keys) == fields, diagnostic || profile != nil,
          !(qualification || diagnostic) || payload.isEmpty else { return response("invalid_request") }
    let now = IPC.now
    guard expires > now else { return qualification || diagnostic ? blocked(.leaseExpired) : response("timeout") }
    guard expires <= now + 300_000 else { return response("invalid_request") }
    let remaining = expires - now
    let deadline = ProcessInfo.processInfo.systemUptime + Double(remaining) / 1000
    let safari = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Safari")
    let running = safari.count == 1 ? safari.first : nil
    let browser = running.flatMap { !$0.isTerminated && $0.isFinishedLaunching ? $0 : nil }
    // Native-only observation: no app group lookup, files, heartbeat, arm or lease.
    if diagnostic { return diagnoseQualification(id: id, browser: browser, expires: expires, deadline: deadline) }
    guard let profile else { return response("invalid_request") }
    let kind = payload["kind"] as? String ?? ""
    let mutation = qualification || operation == "open" || operation == "close" || operation == "tab" || operation == "front_open" ||
        (operation == "page" && IPC.pageMutations.contains(kind))
    let backgroundMutation = operation == "open" || operation == "close" || (operation == "tab" && kind == "navigate")
    let foregroundMutation = operation == "front_open" || (operation == "tab" && kind == "activate")
    let capture = operation == "capture"
    let uncertain = qualification ? "unconfirmed" : operation == "open" ? "open_unconfirmed" : operation == "close" ? "cleanup_unconfirmed" : "action_unconfirmed"
    if browser == nil && !qualification {
        return response(operation == "close" ? "cleanup_instance_changed" : "disconnected")
    }
    let root: URL
    let qualificationLock: Int32?
    do {
        root = try IPC.root(profile)
        // Only one qualification host may be active across all named profiles.
        qualificationLock = qualification ? try IPC.lock(root.deletingLastPathComponent(), "qualify.lock") : nil
    } catch {
        if qualification { return blocked(.mailboxUnavailable) }
        throw error
    }
    defer { if let qualificationLock { close(qualificationLock) } }
    let host: Int32
    do { host = try IPC.lock(root, "host.lock") }
    catch {
        if qualification { return blocked(.mailboxUnavailable) }
        throw error
    }
    defer { close(host) }

    let workspace = NSWorkspace.shared
    let initialFront = workspace.frontmostApplication?.bundleIdentifier
    let frontPID = workspace.frontmostApplication?.processIdentifier
    let safariPID = browser?.processIdentifier ?? 0
    let focus = FocusEvidence()
    var observer: NSObjectProtocol?
    if backgroundMutation || capture || qualification {
        observer = workspace.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil
        ) { note in
            if capture || qualification { focus.mark() }
            else if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
               app.bundleIdentifier == "com.apple.Safari" { focus.mark() }
        }
    }
    defer { if let observer { workspace.notificationCenter.removeObserver(observer) } }
    if capture && workspace.frontmostApplication?.processIdentifier != safariPID {
        return response("action_unavailable")
    }

    func qualificationBackgroundBlock() -> IPC.QualificationBlock? {
        qualificationProcessBlock(browser: browser, frontPID: frontPID, workspace: workspace, focus: focus)
    }

    func checkedResult(_ data: Data) throws -> [String: Any] {
        let result = try IPC.object(data)
        if qualification {
            let answer = IPC.qualificationAnswer(result, id: id)
            if answer["status"] as? String == "candidate_observed", qualificationBackgroundBlock() != nil {
                return response(uncertain)
            }
            return answer
        }
        if capture && result["status"] as? String == "ok" &&
            (focus.observed || workspace.frontmostApplication?.processIdentifier != safariPID) {
            return response("target_changed")
        }
        if foregroundMutation && result["status"] as? String == "ok" &&
           workspace.frontmostApplication?.processIdentifier != safariPID {
            return response(uncertain)
        }
        if backgroundMutation && (result["created"] as? Bool == true || result["status"] as? String == "closed" ||
            (operation == "tab" && result["status"] as? String == "ok")) {
            let front = workspace.frontmostApplication?.bundleIdentifier
            if focus.observed || (initialFront != "com.apple.Safari" && front == "com.apple.Safari") {
                return ["v": IPC.version, "id": id, "status": uncertain, "reason": "safari_activation_observed"]
            }
        }
        return result
    }

    var browserRequest = request
    browserRequest.removeValue(forKey: "profile")
    let bytes = try IPC.json(browserRequest)
    var published = false
    var prepared = false
    var publishedExpiry = expires
    func live() -> Bool { ProcessInfo.processInfo.systemUptime < deadline && IPC.now < publishedExpiry }
    func clearLease() {
        for _ in 0..<8 {
            do {
                try IPC.access(root) { state, update in
                    if state.lease?.id == id { state.lease = nil; update = true }
                }
                return
            } catch { Thread.sleep(forTimeInterval: 0.005) }
        }
    }
    defer { if published || prepared { clearLease() } }

    func stopQualification(_ reason: IPC.QualificationBlock) -> [String: Any] {
        // A snapshot of an unclaimed lease is insufficient: revoke it under the same lock used to claim it.
        do {
            return try IPC.access(root) { state, update in
                guard let lease = state.lease, lease.id == id else { return response(uncertain) }
                if let result = lease.result { return try checkedResult(result) }
                guard lease.claim == nil else { return response(uncertain) }
                state.lease = nil
                update = true
                return blocked(reason)
            }
        } catch { return response(uncertain) }
    }

    while live() {
        do {
            let early: [String: Any]? = try IPC.access(root) { state, update in
                guard live() else { return qualification ? blocked(.leaseExpired) : response("timeout") }
                if capture && (focus.observed || workspace.frontmostApplication?.processIdentifier != safariPID) {
                    return response("target_changed")
                }
                // PID/process discovery is not identity. A write additionally requires a fresh
                // Safari-authenticated heartbeat for the exact process-local instance nonce.
                let instance: UUID?
                let leaseExpiry: Int64
                let armID: UUID?
                if qualification {
                    // Consume before checking the external preconditions or publishing a lease.
                    update = state.qualificationArm != nil
                    let consumed = state.takeQualification(for: profile, now: IPC.now)
                    if let reason = qualificationBackgroundBlock() { return blocked(reason) }
                    guard let arm = consumed else { return blocked(.armMissingOrInvalid) }
                    guard let frontPID else { return blocked(.foregroundUnverified) }
                    let windowBlock = qualificationWindowFailure(pid: safariPID, frontPID: frontPID)?.block
                    if let reason = qualificationBackgroundBlock() { return blocked(reason) }
                    guard live(), IPC.now < arm.expires else { return blocked(.leaseExpired) }
                    if let windowBlock { return blocked(windowBlock) }
                    instance = arm.sender.instance
                    armID = arm.id
                    // Bound the gap between the native snapshot and the extension's observation.
                    leaseExpiry = min(expires, arm.expires, IPC.now + 4000)
                } else {
                    instance = state.presence.flatMap { presence in
                        presence.sender.profile == profile && presence.received <= IPC.now && presence.received >= IPC.now - 4000
                            ? presence.sender.instance : nil
                    }
                    armID = nil
                    leaseExpiry = expires
                }
                if mutation && instance == nil { return response(operation == "close" ? "cleanup_instance_changed" : "disconnected") }
                if mutation, let instance, ["close", "tab", "page"].contains(operation) {
                    let ref = payload["ref"] as? String ?? ""
                    guard ref.lowercased().hasPrefix(instance.uuidString.lowercased() + ".") else {
                        return response(operation == "close" ? "cleanup_instance_changed" : "target_changed")
                    }
                }
                if qualification { browserRequest["expires"] = leaseExpiry }
                state.lease = IPC.Lease(id: id, profile: profile, instance: mutation ? instance : nil,
                                        expires: leaseExpiry, request: qualification ? try IPC.json(browserRequest) : bytes,
                                        claim: nil, result: nil, qualificationArmID: armID)
                prepared = qualification
                publishedExpiry = leaseExpiry
                update = true
                return nil
            }
            if let early { return early }
            published = true
            break
        } catch let error as IPCError where error.status == "busy" {
            Thread.sleep(forTimeInterval: 0.01)
        } catch {
            if qualification { return prepared ? response(uncertain) : blocked(.mailboxUnavailable) }
            throw error
        }
    }
    guard published else { return qualification ? blocked(.leaseExpired) : response("timeout") }

    var claimed = false
    repeat {
        if qualification, let reason = qualificationBackgroundBlock() { return stopQualification(reason) }
        do {
            let lease = try IPC.access(root) { state, _ in state.lease }
            guard let lease, lease.id == id else { return response(mutation ? uncertain : "disconnected") }
            claimed = lease.claim != nil
            if let result = lease.result { return try checkedResult(result) }
        } catch let error as IPCError where error.status == "busy" {
            if !live() {
                if qualification { break }
                return response(mutation ? uncertain : "timeout")
            }
        } catch {
            return response(mutation ? uncertain : "disconnected")
        }
        if !live() { break }
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.025))
    } while true
    if qualification { return stopQualification(.leaseExpired) }
    return response(claimed && mutation ? uncertain : operation == "status" ? "disconnected" : "timeout")
}

func qualificationProcessBlock(browser: NSRunningApplication?, frontPID: pid_t?, workspace: NSWorkspace,
                               focus: FocusEvidence) -> IPC.QualificationBlock? {
    guard let browser, !browser.isTerminated, browser.isFinishedLaunching, browser.processIdentifier > 0 else { return .safariNotRunning }
    guard let frontPID, frontPID > 0, frontPID != browser.processIdentifier, !focus.observed,
          workspace.frontmostApplication?.processIdentifier == frontPID else { return .foregroundUnverified }
    return nil
}

func diagnoseQualification(id: String, browser: NSRunningApplication?, expires: Int64, deadline: TimeInterval) -> [String: Any] {
    let workspace = NSWorkspace.shared
    let frontPID = workspace.frontmostApplication?.processIdentifier
    let focus = FocusEvidence()
    let observer = workspace.notificationCenter.addObserver(
        forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil
    ) { _ in focus.mark() }
    defer { workspace.notificationCenter.removeObserver(observer) }
    func processBlock() -> IPC.QualificationBlock? {
        qualificationProcessBlock(browser: browser, frontPID: frontPID, workspace: workspace, focus: focus)
    }
    if let reason = processBlock() { return reason.response(id: id) }
    guard let browser, let frontPID else { return IPC.QualificationBlock.foregroundUnverified.response(id: id) }
    let failure = qualificationWindowFailure(pid: browser.processIdentifier, frontPID: frontPID)
    if let reason = processBlock() { return reason.response(id: id) }
    guard ProcessInfo.processInfo.systemUptime < deadline, IPC.now < expires else {
        return IPC.QualificationBlock.leaseExpired.response(id: id)
    }
    if let failure { return ["v": IPC.version, "id": id, "status": "qualification_blocked", "reason": failure.rawValue] }
    return ["v": IPC.version, "id": id, "status": "qualification_ready"]
}

enum QualificationWindowFailure: String {
    case screenCaptureUnavailable = "screen_capture_unavailable"
    case foregroundUnverified = "foreground_unverified"
    case quartzSnapshotUnavailable = "quartz_snapshot_unavailable"
    case quartzOwnerUnverified = "quartz_owner_unverified"
    case accessibilityUntrusted = "accessibility_untrusted"
    case accessibilityTimeoutConfigurationFailed = "accessibility_timeout_configuration_failed"
    case accessibilityQueryFailed = "accessibility_query_failed"
    case accessibilityResultInvalid = "accessibility_result_invalid"
    case safariWindowsPresent = "safari_windows_present"

    var block: IPC.QualificationBlock {
        switch self {
        case .screenCaptureUnavailable: return .screenCaptureUnavailable
        case .foregroundUnverified: return .foregroundUnverified
        case .accessibilityUntrusted: return .accessibilityUntrusted
        case .safariWindowsPresent: return .safariWindowsPresent
        case .quartzSnapshotUnavailable, .quartzOwnerUnverified, .accessibilityTimeoutConfigurationFailed,
             .accessibilityQueryFailed, .accessibilityResultInvalid: return .windowSnapshotUnavailable
        }
    }
}

// Quartz must establish positive console/foreground evidence independently of AX.
// Do not filter on name, layer, minimization or profile; no permission requests.
func qualificationWindowFailure(pid: pid_t, frontPID: pid_t) -> QualificationWindowFailure? {
    guard CGPreflightScreenCaptureAccess() else { return .screenCaptureUnavailable }
    guard let session = CGSessionCopyCurrentDictionary() as? [String: Any],
          session["kCGSSessionOnConsoleKey"] as? Bool == true,
          IPC.integer(session["kCGSSessionUserIDKey"]) == Int64(getuid()) else { return .foregroundUnverified }
    let windows = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]]
    return qualificationSurfaceBlock(windows, pid: pid, frontPID: frontPID) {
        // Owned surfaces alone do not establish that Safari has a real window. Resolve
        // only that case, using existing AX trust and a read-only app-wide AXWindows query.
        guard AXIsProcessTrusted() else { return .accessibilityUntrusted }
        let app = AXUIElementCreateApplication(pid)
        var value: CFTypeRef?
        guard AXUIElementSetMessagingTimeout(app, 0.5) == .success else { return .accessibilityTimeoutConfigurationFailed }
        guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success else { return .accessibilityQueryFailed }
        guard let value, CFGetTypeID(value) == CFArrayGetTypeID(), let windows = value as? [Any] else { return .accessibilityResultInvalid }
        return windows.isEmpty ? nil : .safariWindowsPresent
    }
}

func qualificationSurfaceBlock(_ windows: [[String: Any]]?, pid: pid_t, frontPID: pid_t,
                                resolveSafari: () -> QualificationWindowFailure?) -> QualificationWindowFailure? {
    guard let windows, !windows.isEmpty else { return .quartzSnapshotUnavailable }
    var frontObserved = false
    var safariObserved = false
    for window in windows {
        guard let owner = IPC.integer(window[kCGWindowOwnerPID as String]), owner > 0 else { return .quartzOwnerUnverified }
        if owner == Int64(frontPID) { frontObserved = true }
        if owner == Int64(pid) { safariObserved = true }
    }
    guard frontObserved else { return .foregroundUnverified }
    return safariObserved ? resolveSafari() : nil
}

final class FocusEvidence: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    var observed: Bool { lock.lock(); defer { lock.unlock() }; return value }
    func mark() { lock.lock(); defer { lock.unlock() }; value = true }
}

func verifyGate() -> Bool {
    let profile = UUID(), instance = UUID()
    let target = IPC.Binding(profile: profile, instance: instance)
    let wrongProfile = IPC.Binding(profile: UUID(), instance: instance)
    let wrongInstance = IPC.Binding(profile: profile, instance: UUID())
    let id = String(repeating: "a", count: 32)
    let owner = kCGWindowOwnerPID as String
    let front = [owner: 10], safari = [owner: 20]
    func surface(_ windows: [[String: Any]]?, ax: QualificationWindowFailure? = nil) -> (QualificationWindowFailure?, Bool) {
        var queried = false
        let result = qualificationSurfaceBlock(windows, pid: 20, frontPID: 10) { queried = true; return ax }
        return (result, queried)
    }
    let cases: [([[String: Any]]?, QualificationWindowFailure?, QualificationWindowFailure?, Bool)] = [
        (nil, nil, .quartzSnapshotUnavailable, false), ([], nil, .quartzSnapshotUnavailable, false),
        ([safari], nil, .foregroundUnverified, false), ([front, [:]], nil, .quartzOwnerUnverified, false),
        ([front], .accessibilityUntrusted, nil, false), ([front, safari], nil, nil, true),
        ([front, safari], .accessibilityUntrusted, .accessibilityUntrusted, true),
        ([front, safari], .accessibilityTimeoutConfigurationFailed, .accessibilityTimeoutConfigurationFailed, true),
        ([front, safari], .accessibilityQueryFailed, .accessibilityQueryFailed, true),
        ([front, safari], .accessibilityResultInvalid, .accessibilityResultInvalid, true),
        ([front, safari], .safariWindowsPresent, .safariWindowsPresent, true),
    ]
    for (windows, ax, expected, queried) in cases {
        guard surface(windows, ax: ax) == (expected, queried) else { return false }
    }
    for unavailable in [QualificationWindowFailure.quartzSnapshotUnavailable, .quartzOwnerUnverified,
                         .accessibilityTimeoutConfigurationFailed, .accessibilityQueryFailed, .accessibilityResultInvalid] {
        guard unavailable.block == .windowSnapshotUnavailable else { return false }
    }
    let diagnostic: [String: Any] = ["v": IPC.version, "id": id, "operation": "qualify_window_diagnose",
                                     "payload": [String: Any](), "expires": IPC.now - 1]
    var badDiagnostic = diagnostic
    badDiagnostic["profile"] = profile.uuidString
    guard let expired = try? run(diagnostic, id: id),
          expired["status"] as? String == "qualification_blocked", expired["reason"] as? String == "lease_expired",
          let invalid = try? run(badDiagnostic, id: id), invalid["status"] as? String == "invalid_request" else { return false }
    let refusal = IPC.QualificationBlock.safariWindowsPresent.response(id: id)
    let unexpected = refusal.merging(["ref": "unsafe"]) { first, _ in first }
    let nativeOnly = IPC.QualificationBlock.accessibilityUntrusted.response(id: id)
    guard IPC.qualificationAnswer(refusal, id: id)["reason"] as? String == "safari_windows_present",
          IPC.qualificationAnswer(unexpected, id: id)["status"] as? String == "unconfirmed",
          IPC.qualificationAnswer(nativeOnly, id: id)["status"] as? String == "unconfirmed",
          IPC.qualificationAnswer(refusal, id: "wrong")["status"] as? String == "unconfirmed" else { return false }
    // A slash-only base64 chunk must not expand beyond the native message limit.
    let chunk = String(repeating: "/", count: 48_000)
    guard let frame = try? IPC.json(["v": IPC.version, "id": id, "status": "ok", "data": chunk, "next_offset": chunk.count]),
          let decoded = try? IPC.object(frame), decoded["data"] as? String == chunk else { return false }
    var lease = IPC.Lease(id: id, profile: profile, instance: instance, expires: 200, request: Data(), claim: nil, result: nil)
    guard !lease.take(wrongProfile, now: 100), !lease.take(wrongInstance, now: 100),
          !lease.take(target, now: 200), lease.take(target, now: 100), !lease.take(target, now: 100),
          !lease.accepts(wrongProfile, id: id, now: 100), !lease.accepts(wrongInstance, id: id, now: 100),
          !lease.accepts(target, id: "wrong", now: 100),
          !lease.accepts(target, id: id, now: 200), lease.accepts(target, id: id, now: 100) else { return false }
    lease.result = Data()
    guard !lease.accepts(target, id: id, now: 100) else { return false }
    let arm = IPC.QualificationArm(sender: target, id: UUID(), expires: 20_000)
    let fresh = IPC.Presence(sender: target, received: 10_000)
    var qualified = IPC.State(presence: fresh, qualificationArm: arm)
    guard qualified.takeQualification(for: profile, now: 14_000)?.id == arm.id,
          qualified.takeQualification(for: profile, now: 14_000) == nil else { return false }
    for (caller, heartbeat, now) in [(wrongProfile.profile, fresh, Int64(12_000)),
        (profile, IPC.Presence(sender: wrongInstance, received: 10_000), 12_000),
        (profile, fresh, 9000), (profile, fresh, 14_001),
        (profile, IPC.Presence(sender: target, received: 19_999), 20_000)] {
        var refused = IPC.State(presence: heartbeat, qualificationArm: arm)
        guard refused.takeQualification(for: caller, now: now) == nil, refused.qualificationArm == nil else { return false }
    }
    // A read-only lease allows either instance to claim, but binds its result to the claimant.
    var unbound = IPC.Lease(id: id, profile: profile, instance: nil, expires: 200, request: Data(), claim: nil, result: nil)
    return !unbound.take(wrongProfile, now: 100) && unbound.take(wrongInstance, now: 100) &&
        !unbound.take(target, now: 100) && !unbound.accepts(target, id: id, now: 100) &&
        unbound.accepts(wrongInstance, id: id, now: 100)
}

let args = Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }
if args.isEmpty { exit(0) } // App registration only; no Safari launch or dispatch.
if args == ["verify-gate"] { exit(verifyGate() ? 0 : 2) }
guard args == ["ipc"] else { exit(2) }
var id = ""
var result: [String: Any]
do {
    // Internal caller closes stdin after one bounded object. Only a command frame, never code.
    var data = Data()
    while data.count <= IPC.limit, let chunk = try FileHandle.standardInput.read(upToCount: IPC.limit + 1 - data.count), !chunk.isEmpty {
        data.append(chunk)
    }
    let request = try IPC.object(data)
    id = IPC.id(request["id"]) ?? ""
    result = try run(request, id: id)
} catch let error as IPCError {
    result = ["v": IPC.version, "id": id, "status": error.status]
} catch {
    result = ["v": IPC.version, "id": id, "status": "disconnected"]
}
do {
    try FileHandle.standardOutput.write(contentsOf: IPC.json(result))
    try FileHandle.standardOutput.write(contentsOf: Data([0x0a]))
} catch { exit(2) }
