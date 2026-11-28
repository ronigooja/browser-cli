import Foundation
import OSLog
import SafariServices

final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    private let logger = Logger(subsystem: "com.ronigooja.agentbrowser.safariprobe", category: "native-message")

    func beginRequest(with context: NSExtensionContext) {
        let details = (context.inputItems.first as? NSExtensionItem)?.userInfo
        let response: [String: Any]
        do {
            guard let message = details?[SFExtensionMessageKey] as? [String: Any],
                  let instanceString = message["instance"] as? String,
                  let instance = UUID(uuidString: instanceString),
                  let phase = message["phase"] as? String else {
                throw ProbeError("invalid_native_message")
            }
            let profileValue = details?[SFExtensionProfileKey]
            let profile = profileValue as? UUID
            let profileError = profileValue == nil ? "safari_profile_missing" : "safari_profile_not_uuid"
            if phase == "pull" {
                response = try pull(message, profile: profile, profileError: profileError, instance: instance)
            } else {
                guard let profile else { throw ProbeError(profileError) }
                let sender = Binding(profile: profile, instance: instance)
                switch phase {
                case "identify":
                    // Only the extension UI uses this to label the UUID with a visible Safari profile.
                    response = ["status": "identified", "profile": profile.uuidString, "instance": instance.uuidString]
                case "claim", "result":
                    let request = try requestID(message)
                    response = try Mailbox.access(request, update: true) { command in
                        if phase == "claim" { return claim(&command, from: sender, via: .push) }
                        return try store(message, in: &command, from: sender, via: .push)
                    }
                case "pull-result":
                    let request = try requestID(message)
                    response = try Mailbox.access(request, via: .pull, update: true) { command in
                        try store(message, in: &command, from: sender, via: .pull)
                    }
                default: throw ProbeError("invalid_phase")
                }
            }
        } catch let error as ProbeError {
            // Only fixed status codes; never log profile identifiers or message contents.
            logger.notice("Native request rejected: \(error.description, privacy: .public)")
            response = ["status": error.description]
        } catch {
            response = ["status": "mailbox_error"]
        }
        let item = NSExtensionItem()
        item.userInfo = [SFExtensionMessageKey: response]
        context.completeRequest(returningItems: [item], completionHandler: nil)
    }

    private func requestID(_ message: [String: Any]) throws -> UUID {
        guard let request = (message["request"] as? String).flatMap(UUID.init(uuidString:)) else {
            throw ProbeError("invalid_request_id")
        }
        return request
    }

    private func pull(_ message: [String: Any], profile: UUID?, profileError: String,
                      instance: UUID) throws -> [String: Any] {
        guard let wake = message["wake"] as? String, ["alarm", "warm_poll", "qualification_poll"].contains(wake) else {
            throw ProbeError("invalid_pull_wake")
        }
        guard let request = try Mailbox.pendingPull() else { return ["status": "no_pending_pull"] }
        return try Mailbox.access(request, via: .pull, update: true) { command in
            guard !command.expired else { return ["status": "expired"] }
            guard (wake == "qualification_poll") == (command.operation == "qualify-window") else {
                return ["status": "wrong_pull_mode"]
            }
            let decision: [String: Any]
            if let profile {
                decision = claim(&command, from: Binding(profile: profile, instance: instance), via: .pull)
            } else {
                // Record only an unbound diagnostic. No UUID, nonce, grant or result rights.
                decision = ["status": profileError]
            }
            let status = decision["status"] as? String ?? "unknown"
            let started = timestamp(message["backgroundStartedAt"])
            if command.pullObservations.count < 32, !command.pullObservations.contains(where: {
                $0.status == status && $0.wake == wake && $0.backgroundStartedAt == started
            }) {
                command.pullObservations.append(PullObservation(
                    status: status, wake: wake, receivedAt: Date().timeIntervalSince1970,
                    alarmScheduledAt: timestamp(message["alarmScheduledAt"]),
                    backgroundStartedAt: started))
            }
            var reply = decision
            if decision["status"] as? String == "claimed" { reply["request"] = request.uuidString }
            return reply
        }
    }

    private func timestamp(_ value: Any?) -> Double? {
        guard let seconds = value as? Double, seconds.isFinite, seconds > 0, seconds < 1e10 else { return nil }
        return seconds
    }

    private func claim(_ command: inout Command, from sender: Binding, via transport: Transport) -> [String: Any] {
        let status = command.claim(from: sender, via: transport)
        guard status == "claimed" else { return ["status": status] }
        var grant: [String: Any] = [
            "status": status, "operation": command.operation, "profile": sender.profile.uuidString,
            "instance": sender.instance.uuidString, "expires": command.expires,
        ]
        grant["window"] = command.window
        grant["tab"] = command.tab
        grant["marker"] = command.marker
        return grant
    }

    private func store(_ message: [String: Any], in command: inout Command, from sender: Binding,
                       via transport: Transport) throws -> [String: Any] {
        guard !command.expired else { return ["status": "expired"] }
        guard command.acceptsResult(from: sender, via: transport),
              let result = message["result"] as? [String: Any],
              JSONSerialization.isValidJSONObject(result) else {
            throw ProbeError("result_not_from_claimed_instance")
        }
        let bytes = try JSONSerialization.data(withJSONObject: result)
        guard bytes.count <= 8 * 1024 else { throw ProbeError("oversized_result") }
        command.result = bytes
        command.resultReceivedAt = Date().timeIntervalSince1970
        return ["status": "accepted"]
    }
}
