import AppKit
import Darwin
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
    var pullDiscovery = discovery
    var pullRead = Command(expires: deadline, operation: "windows", target: nil, window: nil, tab: nil,
                           readProfile: a.profile)
    var unboundRead = Command(expires: deadline, operation: "windows", target: nil, window: nil, tab: nil)
    var pullWrite = Command(expires: deadline, operation: "add-tab", target: a, window: 1, tab: nil)
    var expiredRead = Command(expires: 0, operation: "windows", target: nil, window: nil, tab: nil,
                               readProfile: a.profile)
    var qualification = Command(expires: deadline, operation: "qualify-window", target: a, window: nil, tab: nil)
    var unboundQualification = Command(expires: deadline, operation: "qualify-window", target: nil, window: nil, tab: nil,
                                        readProfile: a.profile)
    guard pullDiscovery.claim(from: a, via: .pull) == "discovered", !pullDiscovery.claimed,
          !pullDiscovery.acceptsResult(from: a, via: .pull),
          unboundRead.claim(from: a, via: .pull) == "unsupported_pull_operation", !unboundRead.claimed,
          pullWrite.claim(from: a, via: .pull) == "unsupported_pull_operation", !pullWrite.claimed,
          pullRead.claim(from: a) == "invalid_push_target", !pullRead.claimed,
          pullRead.claim(from: b, via: .pull) == "wrong_profile", !pullRead.claimed,
          pullRead.claim(from: stale, via: .pull) == "claimed",
          pullRead.claim(from: a, via: .pull) == "already_claimed",
          !pullRead.acceptsResult(from: a, via: .pull), !pullRead.acceptsResult(from: b, via: .pull),
          !pullRead.acceptsResult(from: stale), pullRead.acceptsResult(from: stale, via: .pull),
          expiredRead.claim(from: a, via: .pull) == "expired" else { throw ProbeError("pull_gate_check_failed") }
    pullRead.result = Data()
    guard !pullRead.acceptsResult(from: stale, via: .pull) else { throw ProbeError("duplicate_pull_result_allowed") }
    guard qualification.claim(from: a) == "invalid_push_target",
          unboundQualification.claim(from: a, via: .pull) == "unsupported_pull_operation",
          qualification.claim(from: b, via: .pull) == "wrong_profile",
          qualification.claim(from: stale, via: .pull) == "wrong_instance", !qualification.claimed,
          qualification.claim(from: a, via: .pull) == "claimed",
          qualification.claim(from: a, via: .pull) == "already_claimed",
          !qualification.acceptsResult(from: a), !qualification.acceptsResult(from: stale, via: .pull),
          qualification.acceptsResult(from: a, via: .pull) else { throw ProbeError("qualification_gate_check_failed") }
    qualification.result = Data()
    guard !qualification.acceptsResult(from: a, via: .pull) else { throw ProbeError("duplicate_qualification_result_allowed") }
    try emit(["status": "gate_check_passed", "liveSafari": false])
}

func run(_ args: [String]) throws -> Int32 {
    guard args.first == "pull" else { return try runProbe(args, via: .push, budget: 5) }
    let pullArgs = args.dropFirst()
    guard pullArgs.first == "--wait-seconds" else {
        return try runProbe(Array(pullArgs), via: .pull, budget: 90)
    }
    guard let value = pullArgs.dropFirst().first, let seconds = Int(value), (1...180).contains(seconds) else {
        throw ProbeError("expected_wait_seconds_between_1_and_180")
    }
    return try runProbe(Array(pullArgs.dropFirst(2)), via: .pull, budget: TimeInterval(seconds))
}

func runProbe(_ args: [String], via transport: Transport, budget: TimeInterval) throws -> Int32 {
    guard let operation = args.first else { throw ProbeError("missing_operation") }
    let target: Binding?
    var readProfile: UUID?
    var window: Int?
    var tab: Int?
    var marker: String?
    if transport == .pull {
        switch operation {
        case "discover":
            guard args.count == 1 else { throw ProbeError("unexpected_arguments") }
            target = nil
        case "windows":
            guard args.count == 2, let profile = UUID(uuidString: args[1]) else {
                throw ProbeError("expected_profile_uuid")
            }
            target = nil
            readProfile = profile
        case "qualify-window":
            guard args.count == 4, args[3] == "--allow-fixture-window",
                  let profile = UUID(uuidString: args[1]), let instance = UUID(uuidString: args[2]) else {
                throw ProbeError("expected_profile_and_instance_uuids_and_allow_fixture_window")
            }
            target = Binding(profile: profile, instance: instance)
        default: throw ProbeError("unsupported_pull_operation")
        }
    } else {
        switch operation {
        case "discover", "verify-gate":
            guard args.count == 1 else { throw ProbeError("unexpected_arguments") }
            target = nil
            if operation == "verify-gate" { try verifyGate(); return 0 }
        case "windows", "inspect", "add-tab":
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
    }

    // Dispatch may launch Safari. Do not call it against a browser which is not running.
    // There remains a quit/dispatch race; this is not an atomic no-launch guarantee.
    let candidates = NSRunningApplication.runningApplications(withBundleIdentifier: safariID)
    guard candidates.count == 1, let safari = candidates.first,
          !safari.isTerminated, safari.isFinishedLaunching else { throw ProbeError("safari_not_running") }
    _ = try Mailbox.root(transport, create: true)
    let hostLock = transport == .pull ? try Mailbox.lockPullHost() : nil
    defer { if let hostLock { close(hostLock) } }
    let request = UUID()

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
    if operation == "qualify-window", frontBefore == safariID || frontBefore == "unknown" {
        throw ProbeError("qualification_requires_another_frontmost_app")
    }
    let delivery = Evidence()
    let publishedAt = Date().timeIntervalSince1970
    let command = Command(expires: publishedAt + budget, operation: operation, target: target,
                          window: window, tab: tab, marker: marker, readProfile: readProfile)
    let directory = try Mailbox.create(request, command, via: transport)
    defer { try? FileManager.default.removeItem(at: directory) }
    let deadline = ProcessInfo.processInfo.systemUptime + budget
    let pending = transport == .pull ? try Mailbox.publishPull(request) : nil
    defer { if let pending { try? FileManager.default.removeItem(at: pending) } }
    if transport == .push {
        dispatchPush(request, delivery: delivery)
    }

    // Collect for the entire lease to retain negative receipts even if the target replies first.
    while ProcessInfo.processInfo.systemUptime < deadline {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))
    }
    var final: Command?
    for _ in 0..<10 {
        do { final = try Mailbox.access(request, via: transport) { $0 }; break }
        catch let error as ProbeError where error.description == Mailbox.busy.description {
            Thread.sleep(forTimeInterval: 0.01)
        }
    }
    guard let final else { throw ProbeError("final_mailbox_contended_outcome_unknown") }
    let result = try final.result.map { try JSONSerialization.jsonObject(with: $0) }
    let frontAfter = workspace.frontmostApplication?.bundleIdentifier ?? "unknown"
    let activation = activationEvents.snapshot
    let safariStillRunning = !safari.isTerminated
    var resultStatus = (result as? [String: Any])?["status"] as? String
    if operation == "qualify-window", resultStatus == "candidate_observed",
       activation.safariActivated || activation.truncated || frontAfter != frontBefore || !safariStillRunning {
        resultStatus = "operation_outcome_unknown"
    }
    // A qualification is evidence for manual review, not a verified background creation.
    let success = operation == "discover" ? !final.receipts.isEmpty : ["ok", "created"].contains(resultStatus ?? "")
    let failure = final.claimed ? "claimed_outcome_unknown" : final.pullObservations.last?.status ?? "no_verified_reply"
    var payload: [String: Any] = [
        "status": resultStatus ?? (success ? "discovered" : failure),
        "transport": transport.rawValue,
        "request": request.uuidString,
        "dispatch": transport == .pull ? "not_called" : delivery.entries.last ?? "completion_not_observed",
        "receipts": final.receipts.map { ["profile": $0.sender.profile.uuidString,
                                          "instance": $0.sender.instance.uuidString, "status": $0.status] },
        "frontBefore": frontBefore,
        "frontAfter": frontAfter,
        "activationEvents": activation.entries,
        "activationEventsTruncated": activation.truncated,
        "safariActivationObserved": activation.safariActivated,
        "safariProcessStillRunning": safariStillRunning,
    ]
    if transport == .pull {
        payload["budgetSeconds"] = budget
        payload["publishedAt"] = publishedAt
        payload["resultReceivedAt"] = final.resultReceivedAt
        payload["pullObservations"] = final.pullObservations.map { observation in
            var entry: [String: Any] = ["status": observation.status, "wake": observation.wake,
                                        "receivedAt": observation.receivedAt]
            entry["alarmScheduledAt"] = observation.alarmScheduledAt
            entry["backgroundStartedAt"] = observation.backgroundStartedAt
            return entry
        }
    }
    payload["result"] = result
    try emit(payload)
    return success ? 0 : 2
}

func dispatchPush(_ request: UUID, delivery: Evidence) {
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
}

final class Evidence: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String] = []
    private var truncated = false
    private var safariActivated = false
    var entries: [String] {
        snapshot.entries
    }
    var snapshot: (entries: [String], truncated: Bool, safariActivated: Bool) {
        lock.lock(); defer { lock.unlock() }
        return (values, truncated, safariActivated)
    }
    func activated(_ value: String) {
        lock.lock(); defer { lock.unlock() }
        if value == safariID { safariActivated = true }
        if values.count < 32 { values.append(value) }
        else { truncated = true }
    }
}

let args = Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }
if args.isEmpty {
    // A plain LaunchServices launch makes the containing app available to Safari, without dispatch.
    exit(0)
}
do { exit(try run(args)) }
catch {
    var errorPayload = ["status": String(describing: error), "transport": args.first == "pull" ? "pull" : "push"]
    if args.first == "pull" { errorPayload["dispatch"] = "not_called" }
    try? emit(errorPayload)
    exit(2)
}
