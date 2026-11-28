import AppKit
import CoreGraphics
import Darwin
import Foundation

func run(_ request: [String: Any], id: String) throws -> [String: Any] {
    func response(_ status: String) -> [String: Any] { ["v": IPC.version, "id": id, "status": status] }
    guard IPC.integer(request["v"]) == IPC.version else { return response("protocol_mismatch") }
    guard Set(request.keys) == ["v", "id", "profile", "operation", "payload", "expires"],
          IPC.id(request["id"]) != nil, let operation = request["operation"] as? String, IPC.operations.contains(operation),
          let profileValue = request["profile"] as? String, let profile = UUID(uuidString: profileValue),
          let payload = request["payload"] as? [String: Any], let expires = IPC.integer(request["expires"]) else { return response("invalid_request") }
    let now = IPC.now
    let qualification = operation == "qualify_window"
    guard !qualification || payload.isEmpty else { return response("invalid_request") }
    guard expires > now else { return response(qualification ? "unconfirmed" : "timeout") }
    guard expires <= now + 300_000 else { return response("invalid_request") }
    let remaining = expires - now
    let deadline = ProcessInfo.processInfo.systemUptime + Double(remaining) / 1000
    let kind = payload["kind"] as? String ?? ""
    let mutation = qualification || operation == "open" || operation == "close" || operation == "tab" || operation == "front_open" ||
        (operation == "page" && IPC.pageMutations.contains(kind))
    let backgroundMutation = operation == "open" || operation == "close" || (operation == "tab" && kind == "navigate")
    let foregroundMutation = operation == "front_open" || (operation == "tab" && kind == "activate")
    let capture = operation == "capture"
    let uncertain = qualification ? "unconfirmed" : operation == "open" ? "open_unconfirmed" : operation == "close" ? "cleanup_unconfirmed" : "action_unconfirmed"
    let safari = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Safari")
    let running = safari.count == 1 ? safari.first : nil
    let browser = running.flatMap { !$0.isTerminated && $0.isFinishedLaunching ? $0 : nil }
    if browser == nil && !qualification {
        return response(operation == "close" ? "cleanup_instance_changed" : "disconnected")
    }
    let root = try IPC.root(profile)
    // Only one qualification host may be active across all named profiles.
    let qualificationLock = qualification ? try IPC.lock(root.deletingLastPathComponent(), "qualify.lock") : nil
    defer { if let qualificationLock { close(qualificationLock) } }
    let host = try IPC.lock(root, "host.lock")
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

    func qualificationStillBackground() -> Bool {
        guard let browser, !browser.isTerminated, browser.isFinishedLaunching,
              let frontPID, frontPID > 0, frontPID != safariPID, safariPID > 0, !focus.observed else { return false }
        return workspace.frontmostApplication?.processIdentifier == frontPID
    }

    func checkedResult(_ data: Data) throws -> [String: Any] {
        let result = try IPC.object(data)
        if qualification {
            return response(result["status"] as? String == "candidate_observed" && qualificationStillBackground()
                            ? "candidate_observed" : "unconfirmed")
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
    defer { if published { clearLease() } }

    while live() {
        do {
            let early: String? = try IPC.access(root) { state, update in
                guard live() else { return qualification ? uncertain : "timeout" }
                if capture && (focus.observed || workspace.frontmostApplication?.processIdentifier != safariPID) {
                    return "target_changed"
                }
                // PID/process discovery is not identity. A write additionally requires a fresh
                // Safari-authenticated heartbeat for the exact process-local instance nonce.
                let instance: UUID?
                let leaseExpiry: Int64
                let armID: UUID?
                if qualification {
                    // Consume before checking the external preconditions or publishing a lease.
                    update = state.qualificationArm != nil
                    guard let arm = state.takeQualification(for: profile, now: IPC.now),
                          qualificationStillBackground(), let frontPID, qualificationNoWindows(pid: safariPID, frontPID: frontPID),
                          qualificationStillBackground(), live(), IPC.now < arm.expires else { return uncertain }
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
                if mutation && instance == nil { return operation == "close" ? "cleanup_instance_changed" : "disconnected" }
                if mutation, let instance, ["close", "tab", "page"].contains(operation) {
                    let ref = payload["ref"] as? String ?? ""
                    guard ref.lowercased().hasPrefix(instance.uuidString.lowercased() + ".") else {
                        return operation == "close" ? "cleanup_instance_changed" : "target_changed"
                    }
                }
                if qualification { browserRequest["expires"] = leaseExpiry }
                state.lease = IPC.Lease(id: id, profile: profile, instance: mutation ? instance : nil,
                                        expires: leaseExpiry, request: qualification ? try IPC.json(browserRequest) : bytes,
                                        claim: nil, result: nil, qualificationArmID: armID)
                publishedExpiry = leaseExpiry
                update = true
                return nil
            }
            if let early { return response(early) }
            published = true
            break
        } catch let error as IPCError where error.status == "busy" {
            Thread.sleep(forTimeInterval: 0.01)
        }
    }
    guard published else { return response(qualification ? uncertain : "timeout") }

    var claimed = false
    repeat {
        if qualification && !qualificationStillBackground() { return response(uncertain) }
        do {
            let lease = try IPC.access(root) { state, _ in state.lease }
            guard let lease, lease.id == id else { return response(mutation ? uncertain : "disconnected") }
            claimed = lease.claim != nil
            if let result = lease.result { return try checkedResult(result) }
        } catch let error as IPCError where error.status == "busy" {
            if !live() { return response(mutation ? uncertain : "timeout") }
        } catch {
            return response(mutation ? uncertain : "disconnected")
        }
        if !live() { break }
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.025))
    } while true
    return response(qualification || (claimed && mutation) ? uncertain : operation == "status" ? "disconnected" : "timeout")
}

// Independent snapshot of all WindowServer surfaces, including off-screen ones.
// Do not filter on name, layer, minimization or profile. Ambiguity fails closed.
// Preflight never asks for screen-recording access; do not request access here.
func qualificationNoWindows(pid: pid_t, frontPID: pid_t) -> Bool {
    guard CGPreflightScreenCaptureAccess(),
          let session = CGSessionCopyCurrentDictionary() as? [String: Any],
          session["kCGSSessionOnConsoleKey"] as? Bool == true,
          IPC.integer(session["kCGSSessionUserIDKey"]) == Int64(getuid()),
          let windows = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]],
          !windows.isEmpty else { return false }
    var frontObserved = false
    for window in windows {
        guard let owner = IPC.integer(window[kCGWindowOwnerPID as String]), owner > 0, owner != Int64(pid) else { return false }
        if owner == Int64(frontPID) { frontObserved = true }
    }
    return frontObserved
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
