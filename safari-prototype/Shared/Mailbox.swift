import Darwin
import Foundation

struct ProbeError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

struct Binding: Codable, Equatable {
    let profile: UUID
    let instance: UUID
}

struct Receipt: Codable {
    let sender: Binding
    let status: String
}

struct Command: Codable {
    let expires: Double
    let operation: String
    let target: Binding?
    let window: Int?
    let tab: Int?
    var marker: String?
    var receipts: [Receipt] = []
    var claimed = false
    // A bounded JSON object, produced only by the bridge after Safari verifies the sender.
    var result: Data?

    var expired: Bool { Date().timeIntervalSince1970 >= expires }

    mutating func claim(from sender: Binding) -> String {
        guard !expired else { return "expired" }
        let status: String
        if operation == "discover" { status = "discovered" }
        else if target?.profile != sender.profile { status = "wrong_profile" }
        else if target?.instance != sender.instance { status = "wrong_instance" }
        else if claimed { status = "already_claimed" }
        else {
            status = "claimed"
            claimed = true
        }
        if receipts.count < 32,
           !receipts.contains(where: { $0.sender == sender && $0.status == status }) {
            receipts.append(Receipt(sender: sender, status: status))
        }
        return status
    }

    func acceptsResult(from sender: Binding) -> Bool {
        !expired && claimed && target == sender && result == nil
    }
}

enum Mailbox {
    static let maximum = 24 * 1024
    static let busy = ProbeError("busy")

    private static func privateDirectory(_ url: URL) throws {
        var info = stat()
        guard lstat(url.path, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR),
              info.st_mode & 0o777 == 0o700, info.st_uid == geteuid() else {
            throw ProbeError("group_mailbox_not_private_or_unavailable")
        }
    }

    static func root(create: Bool = false) throws -> URL {
        guard let identifier = Bundle.main.object(forInfoDictionaryKey: "ProbeAppGroup") as? String,
              let group = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: identifier) else {
            throw ProbeError("app_group_not_configured")
        }
        let root = group.appendingPathComponent("agent-browser-safari-probe-v1", isDirectory: true)
        if create, mkdir(root.path, 0o700) != 0, errno != EEXIST {
            throw ProbeError("cannot_create_group_mailbox")
        }
        try privateDirectory(root)
        return root
    }

    static func directory(_ id: UUID) throws -> URL {
        try root().appendingPathComponent(id.uuidString, isDirectory: true)
    }

    static func create(_ id: UUID, _ command: Command) throws -> URL {
        let dir = try directory(id)
        guard mkdir(dir.path, 0o700) == 0 else { throw ProbeError("cannot_create_request") }
        do {
            let fd = open(dir.appendingPathComponent("lock").path,
                          O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
            guard fd >= 0 else { throw ProbeError("cannot_create_request_lock") }
            close(fd)
            try write(command, in: dir)
            return dir
        } catch {
            try? FileManager.default.removeItem(at: dir)
            throw error
        }
    }

    private static func write(_ command: Command, in dir: URL) throws {
        let bytes = try JSONEncoder().encode(command)
        guard bytes.count <= maximum else { throw ProbeError("oversized_mailbox") }
        try bytes.write(to: dir.appendingPathComponent("state.json"), options: .atomic)
    }

    // The request directory is private to the current UID and the signed app group (not a
    // defense against the same OS user). Its lock inode is never replaced during the lease.
    static func access<T>(_ id: UUID, update: Bool = false,
                          _ body: (inout Command) throws -> T) throws -> T {
        let dir = try directory(id)
        try privateDirectory(dir)
        let lock = open(dir.appendingPathComponent("lock").path, O_RDWR | O_NOFOLLOW | O_NONBLOCK)
        guard lock >= 0 else { throw ProbeError("unknown_request") }
        defer { close(lock) }
        var info = stat()
        guard fstat(lock, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
              info.st_mode & 0o777 == 0o600, info.st_uid == geteuid() else {
            throw ProbeError("invalid_request_lock")
        }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else {
            if errno == EWOULDBLOCK { throw busy }
            throw ProbeError("request_lock_failed")
        }
        let fd = open(dir.appendingPathComponent("state.json").path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { throw ProbeError("unknown_request_state") }
        let file = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? file.close() }
        guard fstat(fd, &info) == 0, info.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), info.st_uid == geteuid(),
              let bytes = try file.read(upToCount: maximum + 1), bytes.count <= maximum else {
            throw ProbeError("invalid_request_state")
        }
        var command = try JSONDecoder().decode(Command.self, from: bytes)
        let result = try body(&command)
        if update { try write(command, in: dir) }
        return result
    }
}
