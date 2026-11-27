import AppKit
import Darwin
import Foundation

func run(_ request: [String: Any], id: String) throws -> [String: Any] {
    func response(_ status: String) -> [String: Any] { ["v": 1, "id": id, "status": status] }
    guard IPC.integer(request["v"]) == 1, Set(request.keys) == ["v", "id", "profile", "operation", "payload", "expires"],
          IPC.id(request["id"]) != nil, let operation = request["operation"] as? String, IPC.operations.contains(operation),
          let profileValue = request["profile"] as? String, let profile = UUID(uuidString: profileValue),
          request["payload"] is [String: Any], let expires = IPC.integer(request["expires"]) else { return response("invalid_request") }
    let now = IPC.now
    guard expires > now else { return response("timeout") }
    guard expires <= now + 300_000 else { return response("invalid_request") }
    let remaining = expires - now
    let deadline = ProcessInfo.processInfo.systemUptime + Double(remaining) / 1000
    let mutation = operation == "open" || operation == "close"
    let uncertain = operation == "open" ? "open_unconfirmed" : "cleanup_unconfirmed"
    let safari = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Safari")
    guard safari.count == 1, let browser = safari.first, !browser.isTerminated, browser.isFinishedLaunching else {
        return response(operation == "close" ? "cleanup_instance_changed" : "disconnected")
    }
    let root = try IPC.root(profile)
    let host = try IPC.lock(root, "host.lock")
    defer { close(host) }

    let workspace = NSWorkspace.shared
    let initialFront = workspace.frontmostApplication?.bundleIdentifier
    let focus = FocusEvidence()
    var observer: NSObjectProtocol?
    if mutation {
        observer = workspace.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: nil
        ) { note in
            if let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
               app.bundleIdentifier == "com.apple.Safari" { focus.mark() }
        }
    }
    defer { if let observer { workspace.notificationCenter.removeObserver(observer) } }

    func checkedResult(_ data: Data) throws -> [String: Any] {
        let result = try IPC.object(data)
        if mutation && (result["created"] as? Bool == true || result["status"] as? String == "closed") {
            let front = workspace.frontmostApplication?.bundleIdentifier
            if focus.observed || (initialFront != "com.apple.Safari" && front == "com.apple.Safari") {
                return ["v": 1, "id": id, "status": uncertain, "reason": "safari_activation_observed"]
            }
        }
        return result
    }

    var browserRequest = request
    browserRequest.removeValue(forKey: "profile")
    let bytes = try IPC.json(browserRequest)
    var published = false
    func live() -> Bool { ProcessInfo.processInfo.systemUptime < deadline && IPC.now < expires }
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
                guard live() else { return "timeout" }
                // PID/process discovery is not identity. A write additionally requires a fresh
                // Safari-authenticated heartbeat for the exact process-local instance nonce.
                let instance = state.presence.flatMap { presence in
                    presence.sender.profile == profile && presence.received <= IPC.now && presence.received >= IPC.now - 4000
                        ? presence.sender.instance : nil
                }
                if mutation && instance == nil { return operation == "close" ? "cleanup_instance_changed" : "disconnected" }
                if operation == "close", let instance {
                    let ref = (browserRequest["payload"] as? [String: Any])?["ref"] as? String ?? ""
                    guard ref.lowercased().hasPrefix(instance.uuidString.lowercased() + ".") else { return "cleanup_instance_changed" }
                }
                state.lease = IPC.Lease(id: id, profile: profile, instance: mutation ? instance : nil,
                                        expires: expires, request: bytes, claim: nil, result: nil)
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
    guard published else { return response("timeout") }

    var claimed = false
    repeat {
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
    return response(claimed && mutation ? uncertain : operation == "status" ? "disconnected" : "timeout")
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
    var lease = IPC.Lease(id: id, profile: profile, instance: instance, expires: 200, request: Data(), claim: nil, result: nil)
    guard !lease.take(wrongProfile, now: 100), !lease.take(wrongInstance, now: 100),
          !lease.take(target, now: 200), lease.take(target, now: 100), !lease.take(target, now: 100),
          !lease.accepts(wrongProfile, id: id, now: 100), !lease.accepts(wrongInstance, id: id, now: 100),
          !lease.accepts(target, id: "wrong", now: 100),
          !lease.accepts(target, id: id, now: 200), lease.accepts(target, id: id, now: 100) else { return false }
    lease.result = Data()
    guard !lease.accepts(target, id: id, now: 100) else { return false }
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
    result = ["v": 1, "id": id, "status": error.status]
} catch {
    result = ["v": 1, "id": id, "status": "disconnected"]
}
do {
    try FileHandle.standardOutput.write(contentsOf: IPC.json(result))
    try FileHandle.standardOutput.write(contentsOf: Data([0x0a]))
} catch { exit(2) }
