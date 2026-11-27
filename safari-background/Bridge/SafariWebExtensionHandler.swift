import Foundation
import SafariServices

final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    func beginRequest(with context: NSExtensionContext) {
        let details = (context.inputItems.first as? NSExtensionItem)?.userInfo
        let response: [String: Any]
        do {
            guard let profileValue = details?[SFExtensionProfileKey] else { throw IPCError("safari_profile_missing") }
            guard let profile = profileValue as? UUID else { throw IPCError("safari_profile_not_uuid") }
            guard let message = details?[SFExtensionMessageKey] as? [String: Any], IPC.integer(message["v"]) == 1,
                  let nonce = message["instance"] as? String, let instance = UUID(uuidString: nonce),
                  let phase = message["phase"] as? String else { throw IPCError("invalid_request") }
            let sender = IPC.Binding(profile: profile, instance: instance)
            if phase == "identify" {
                response = ["status": "identified", "profile": profile.uuidString]
            } else {
                response = try exchange(phase, message: message, sender: sender)
            }
        } catch let error as IPCError {
            response = ["status": error.status]
        } catch {
            response = ["status": "mailbox_unavailable"]
        }
        let item = NSExtensionItem()
        item.userInfo = [SFExtensionMessageKey: response]
        context.completeRequest(returningItems: [item], completionHandler: nil)
    }

    private func exchange(_ phase: String, message: [String: Any], sender: IPC.Binding) throws -> [String: Any] {
        guard phase == "pull" || phase == "result" else { throw IPCError("invalid_request") }
        guard let selected = (message["selectedProfile"] as? String).flatMap(UUID.init(uuidString:)),
              selected == sender.profile else { throw IPCError("wrong_profile") }
        return try IPC.access(IPC.root(sender.profile)) { state, update in
            let now = IPC.now
            if phase == "pull" {
                if let lease = state.lease, lease.expires <= now { state.lease = nil; update = true }
                // Only the browser-verified profile chooses a directory. No message can select a profile.
                state.presence = IPC.Presence(sender: sender, received: now)
                update = true
                guard var lease = state.lease, lease.take(sender, now: now) else { return ["status": "empty"] }
                let request = try IPC.object(lease.request)
                state.lease = lease // Commit before releasing the grant.
                return ["status": "claimed", "profile": sender.profile.uuidString, "instance": sender.instance.uuidString,
                        "request": request]
            }
            guard var lease = state.lease, let id = IPC.id(message["id"]), lease.accepts(sender, id: id, now: now),
                  let answer = message["answer"] as? [String: Any], IPC.integer(answer["v"]) == 1,
                  answer["id"] as? String == id, answer["status"] is String else { throw IPCError("result_rejected") }
            lease.result = try IPC.json(answer)
            state.lease = lease
            update = true
            return ["status": "accepted"]
        }
    }
}
