import AppKit
import Foundation
import SafariServices

private let extensionID = "com.ronigooja.agentbrowser.safariprobe.extension"
private let safariID = "com.apple.Safari"

func emit(_ payload: [String: Any]) throws {
    let bytes = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
    print(String(decoding: bytes, as: UTF8.self))
}

func verifyGate() throws {
    let a = Binding(profile: UUID(), instance: UUID())
    let b = Binding(profile: UUID(), instance: a.instance)
    let stale = Binding(profile: a.profile, instance: UUID())
    let deadline = Date().timeIntervalSince1970 + 60
    var discovery = Command(expires: deadline, operation: "discover", target: nil, window: nil, tab: nil)
    var bound = Command(expires: deadline, operation: "add-tab", target: a, window: 1, tab: nil)
    var expired = Command(expires: 0, operation: "add-tab", target: a, window: 1, tab: nil)
    guard discovery.claim(from: a) == "discovered", discovery.claim(from: b) == "discovered",
          !discovery.claimed, !discovery.acceptsResult(from: a),
          !bound.acceptsResult(from: a), bound.claim(from: b) == "wrong_profile", !bound.claimed,
          bound.claim(from: stale) == "wrong_instance", !bound.claimed,
          bound.claim(from: a) == "claimed", bound.claim(from: a) == "already_claimed",
          bound.acceptsResult(from: a), !bound.acceptsResult(from: b), !bound.acceptsResult(from: stale),
          expired.claim(from: a) == "expired", !expired.acceptsResult(from: a) else {
        throw ProbeError("gate_check_failed")
    }
    bound.result = Data()
    guard !bound.acceptsResult(from: a) else { throw ProbeError("duplicate_result_allowed") }
    try emit(["status": "gate_check_passed", "liveSafari": false])
}

func run(_ args: [String]) throws -> Int32 {
    guard let operation = args.first else { throw ProbeError("missing_operation") }
    let target: Binding?
    var window: Int?
    var tab: Int?
    var marker: String?
    switch operation {
    case "discover", "verify-gate":
        guard args.count == 1 else { throw ProbeError("unexpected_arguments") }
        target = nil
        if operation == "verify-gate" { try verifyGate(); return 0 }
    case "windows", "inspect", "add-tab", "new-window":
        let needsID = operation == "inspect" || operation == "add-tab"
        guard args.count == (needsID ? 5 : 3),
              let profile = UUID(uuidString: args[1]), let instance = UUID(uuidString: args[2]) else {
            throw ProbeError("expected_profile_and_instance_uuids")
        }
        target = Binding(profile: profile, instance: instance)
        if needsID {
            guard let id = Int(args[3]), id >= 0, id <= Int(Int32.max) else { throw ProbeError("invalid_browser_id") }
            guard ["a", "b"].contains(args[4]) else { throw ProbeError("expected_fixture_marker_a_or_b") }
            marker = args[4]
            if operation == "inspect" { tab = id }
            else { window = id }
        }
    default: throw ProbeError("unknown_operation")
    }

    // Dispatch may launch Safari. Do not call it against a browser which is not running.
    // There remains a quit/dispatch race; this is not an atomic no-launch guarantee.
    guard let safari = NSRunningApplication.runningApplications(withBundleIdentifier: safariID).first,
          !safari.isTerminated, safari.isFinishedLaunching else { throw ProbeError("safari_not_running") }
    _ = try Mailbox.root(create: true)
    let request = UUID()
    let budget: TimeInterval = 5
    let command = Command(expires: Date().timeIntervalSince1970 + budget, operation: operation,
                          target: target, window: window, tab: tab, marker: marker)
    let directory = try Mailbox.create(request, command)
    defer { try? FileManager.default.removeItem(at: directory) }
    let deadline = ProcessInfo.processInfo.systemUptime + budget

    let workspace = NSWorkspace.shared
    let activationEvents = Evidence()
    let observer = workspace.notificationCenter.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                                             object: nil, queue: nil) { notification in
        if let application = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication {
            activationEvents.activated(application.bundleIdentifier ?? "unknown")
        }
    }
    defer { workspace.notificationCenter.removeObserver(observer) }
    let frontBefore = workspace.frontmostApplication?.bundleIdentifier ?? "unknown"
    let delivery = Evidence()
    SFSafariApplication.dispatchMessage(withName: "agent-browser-safari-probe-v1",
                                        toExtensionWithIdentifier: extensionID,
                                        userInfo: ["request": request.uuidString]) { error in
        if let error {
            let nativeError = error as NSError
            delivery.activated("error:\(nativeError.domain):\(nativeError.code)")
        } else {
            delivery.activated("completion_without_error")
        }
    }

    // Collect for the entire lease to retain negative receipts even if the target replies first.
    while ProcessInfo.processInfo.systemUptime < deadline {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
    }
    var final: Command?
    for _ in 0..<10 {
        do { final = try Mailbox.access(request) { $0 }; break }
        catch let error as ProbeError where error.description == Mailbox.busy.description {
            Thread.sleep(forTimeInterval: 0.01)
        }
    }
    guard let final else { throw ProbeError("final_mailbox_contended_outcome_unknown") }
    let result = try final.result.map { try JSONSerialization.jsonObject(with: $0) }
    let resultStatus = (result as? [String: Any])?["status"] as? String
    let success = operation == "discover" ? !final.receipts.isEmpty : ["ok", "created"].contains(resultStatus ?? "")
    var payload: [String: Any] = [
        "status": resultStatus ?? (success ? "discovered" : final.claimed ? "claimed_outcome_unknown" : "no_verified_reply"),
        "request": request.uuidString,
        "dispatch": delivery.entries.last ?? "completion_not_observed",
        "receipts": final.receipts.map { ["profile": $0.sender.profile.uuidString,
                                          "instance": $0.sender.instance.uuidString, "status": $0.status] },
        "frontBefore": frontBefore,
        "frontAfter": workspace.frontmostApplication?.bundleIdentifier ?? "unknown",
        "activationEvents": activationEvents.entries,
    ]
    payload["result"] = result
    try emit(payload)
    return success ? 0 : 2
}

final class Evidence: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String] = []
    var entries: [String] {
        lock.lock(); defer { lock.unlock() }
        return values
    }
    func activated(_ value: String) {
        lock.lock(); defer { lock.unlock() }
        if values.count < 32 { values.append(value) }
    }
}

let args = Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }
if args.isEmpty {
    // A plain LaunchServices launch makes the containing app available to Safari, without dispatch.
    exit(0)
}
do { exit(try run(args)) }
catch {
    try? emit(["status": String(describing: error)])
    exit(2)
}
