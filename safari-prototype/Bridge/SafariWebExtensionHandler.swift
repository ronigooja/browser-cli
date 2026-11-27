import Foundation
import SafariServices

final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    func beginRequest(with context: NSExtensionContext) {
        let details = (context.inputItems.first as? NSExtensionItem)?.userInfo
        let response: [String: Any]
        do {
            guard let profile = details?[SFExtensionProfileKey] as? UUID,
                  let message = details?[SFExtensionMessageKey] as? [String: Any],
                  let instanceString = message["instance"] as? String,
                  let instance = UUID(uuidString: instanceString),
                  let phase = message["phase"] as? String else {
                throw ProbeError("missing_safari_profile_or_invalid_message")
            }
            let sender = Binding(profile: profile, instance: instance)
            if phase == "identify" {
                // Only the extension UI uses this, to associate a human-visible profile with the UUID.
                response = ["status": "identified", "profile": profile.uuidString, "instance": instance.uuidString]
            } else {
                guard let request = (message["request"] as? String).flatMap(UUID.init(uuidString:)) else {
                    throw ProbeError("invalid_request_id")
                }
                response = try Mailbox.access(request, update: true) { command in
                    guard !command.expired else { return ["status": "expired"] }
                    if phase == "claim" {
                        let status = command.claim(from: sender)
                        guard status == "claimed" else { return ["status": status] }
                        var grant: [String: Any] = [
                            "status": status, "operation": command.operation, "profile": profile.uuidString,
                            "instance": instance.uuidString, "expires": command.expires,
                        ]
                        grant["window"] = command.window
                        grant["tab"] = command.tab
                        grant["marker"] = command.marker
                        return grant
                    }
                    guard phase == "result", command.acceptsResult(from: sender),
                          let result = message["result"] as? [String: Any],
                          JSONSerialization.isValidJSONObject(result) else {
                        throw ProbeError("result_not_from_claimed_instance")
                    }
                    let bytes = try JSONSerialization.data(withJSONObject: result)
                    guard bytes.count <= 8 * 1024 else { throw ProbeError("oversized_result") }
                    command.result = bytes
                    return ["status": "accepted"]
                }
            }
        } catch let error as ProbeError {
            response = ["status": error.description]
        } catch {
            response = ["status": "mailbox_error"]
        }
        let item = NSExtensionItem()
        item.userInfo = [SFExtensionMessageKey: response]
        context.completeRequest(returningItems: [item], completionHandler: nil)
    }
}
