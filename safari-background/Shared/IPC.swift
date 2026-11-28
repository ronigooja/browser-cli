import CoreFoundation
import Darwin
import Foundation

struct IPCError: Error { let status: String; init(_ status: String) { self.status = status } }

enum IPC {
    static let version: Int64 = 3
    static let limit = 65_536
    static let stateLimit = 262_144
    static let operations: Set<String> = ["status", "find", "open", "read", "close", "list", "page", "tab", "front_open", "capture", "capture_chunk", "qualify_window"]
    static let qualificationLifetime: Int64 = 120_000
    static let pageMutations: Set<String> = ["click", "scroll", "fill", "select", "press", "submit"]
    static var now: Int64 { Int64(Date().timeIntervalSince1970 * 1000) }

    enum QualificationBlock: String {
        case armMissingOrInvalid = "arm_missing_or_invalid"
        case safariNotRunning = "safari_not_running"
        case foregroundUnverified = "foreground_unverified"
        case screenCaptureUnavailable = "screen_capture_unavailable"
        case windowSnapshotUnavailable = "window_snapshot_unavailable"
        case accessibilityUntrusted = "accessibility_untrusted"
        case safariWindowsPresent = "safari_windows_present"
        case leaseExpired = "lease_expired"
        case permissionsUnverified = "permissions_unverified"
        case mailboxUnavailable = "mailbox_unavailable"

        func response(id: String) -> [String: Any] {
            ["v": IPC.version, "id": id, "status": "qualification_blocked", "reason": rawValue]
        }

        // Only these refusals can be attested by the browser-authenticated extension.
        var allowedFromExtension: Bool {
            switch self {
            case .armMissingOrInvalid, .foregroundUnverified, .windowSnapshotUnavailable,
                 .safariWindowsPresent, .leaseExpired, .permissionsUnverified: return true
            default: return false
            }
        }
    }

    static func qualificationAnswer(_ answer: [String: Any], id: String) -> [String: Any] {
        if integer(answer["v"]) == version, answer["id"] as? String == id {
            let status = answer["status"] as? String
            if Set(answer.keys) == ["v", "id", "status"], status == "candidate_observed" {
                return ["v": version, "id": id, "status": "candidate_observed"]
            }
            if Set(answer.keys) == ["v", "id", "status", "reason"], status == "qualification_blocked",
               let raw = answer["reason"] as? String, let reason = QualificationBlock(rawValue: raw), reason.allowedFromExtension {
                return reason.response(id: id)
            }
        }
        return ["v": version, "id": id, "status": "unconfirmed"]
    }

    struct Binding: Codable, Equatable {
        let profile: UUID
        let instance: UUID
    }

    struct Presence: Codable {
        let sender: Binding
        let received: Int64
    }

    struct QualificationArm: Codable {
        let sender: Binding
        let id: UUID
        let expires: Int64
    }

    struct Lease: Codable {
        let id: String
        let profile: UUID
        let instance: UUID?
        let expires: Int64
        let request: Data
        var claim: Binding?
        var result: Data?
        var qualificationArmID: UUID? = nil

        mutating func take(_ sender: Binding, now: Int64) -> Bool {
            guard expires > now, profile == sender.profile, claim == nil,
                  instance == nil || instance == sender.instance else { return false }
            claim = sender
            return true
        }

        func accepts(_ sender: Binding, id: String, now: Int64) -> Bool {
            expires > now && self.id == id && claim == sender && result == nil
        }
    }

    struct State: Codable {
        var presence: Presence? = nil
        var lease: Lease? = nil
        var qualificationArm: QualificationArm? = nil

        mutating func takeQualification(for profile: UUID, now: Int64) -> QualificationArm? {
            let arm = qualificationArm
            qualificationArm = nil
            guard let arm, arm.expires > now, arm.sender.profile == profile,
                  let presence, presence.sender == arm.sender,
                  presence.received <= now, presence.received >= now - 4000 else { return nil }
            return arm
        }
    }

    static func json(_ value: [String: Any]) throws -> Data {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.withoutEscapingSlashes])
        guard data.count <= limit else { throw IPCError("response_too_large") }
        return data
    }

    static func object(_ input: Data) throws -> [String: Any] {
        guard input.count <= limit, let value = try JSONSerialization.jsonObject(with: input) as? [String: Any] else {
            throw IPCError("invalid_request")
        }
        return value
    }

    static func integer(_ value: Any?) -> Int64? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(),
              let i = Int64(exactly: n.doubleValue) else { return nil }
        return i
    }

    static func id(_ value: Any?) -> String? {
        guard let string = value as? String, string.utf8.count == 32,
              string.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else { return nil }
        return string
    }

    static func root(_ profile: UUID) throws -> URL {
        guard let groupID = Bundle.main.object(forInfoDictionaryKey: "AgentBrowserAppGroup") as? String,
              let group = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: groupID) else {
            throw IPCError("app_group_unavailable")
        }
        // Do not accept a heartbeat or claim from an older installed extension.
        let parent = group.appendingPathComponent("agent-browser-ipc-v\(version)", isDirectory: true)
        try privateDirectory(parent)
        let root = parent.appendingPathComponent(profile.uuidString, isDirectory: true)
        try privateDirectory(root)
        return root
    }

    private static func privateDirectory(_ url: URL) throws {
        guard mkdir(url.path, 0o700) == 0 || errno == EEXIST else { throw IPCError("mailbox_unavailable") }
        var info = stat()
        guard lstat(url.path, &info) == 0, info.st_uid == geteuid(),
              info.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR), info.st_mode & 0o777 == 0o700 else {
            throw IPCError("unsafe_mailbox")
        }
    }

    static func lock(_ root: URL, _ name: String) throws -> Int32 {
        let fd = open(root.appendingPathComponent(name).path, O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0o600)
        guard fd >= 0 else { throw IPCError("mailbox_unavailable") }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == geteuid(),
              info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), info.st_mode & 0o777 == 0o600 else {
            close(fd)
            throw IPCError("unsafe_mailbox")
        }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            let status = errno == EWOULDBLOCK ? "busy" : "mailbox_unavailable"
            close(fd)
            throw IPCError(status)
        }
        return fd
    }

    static func access<T>(_ root: URL, _ body: (inout State, inout Bool) throws -> T) throws -> T {
        let lock = try lock(root, "state.lock")
        defer { close(lock) }
        let path = root.appendingPathComponent("state.json")
        var state = try readState(path)
        var update = false
        let result = try body(&state, &update)
        if update {
            if state.lease == nil && state.presence == nil && state.qualificationArm == nil {
                if unlink(path.path) != 0 && errno != ENOENT { throw IPCError("mailbox_unavailable") }
            } else {
                let bytes = try JSONEncoder().encode(state)
                guard bytes.count <= stateLimit else { throw IPCError("mailbox_unavailable") }
                try bytes.write(to: path, options: .atomic)
            }
        }
        return result
    }

    private static func readState(_ path: URL) throws -> State {
        let fd = open(path.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        if fd < 0 && errno == ENOENT { return State() }
        guard fd >= 0 else { throw IPCError("mailbox_unavailable") }
        let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? file.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_uid == geteuid(), info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
              let data = try file.read(upToCount: stateLimit + 1), data.count <= stateLimit else {
            throw IPCError("unsafe_mailbox")
        }
        return try JSONDecoder().decode(State.self, from: data)
    }
}
