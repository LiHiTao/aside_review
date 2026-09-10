"""Anonymized fixture for three UIKit entry forms sharing a legal page."""
from pathlib import Path


SOURCES = {
    "Document.swift": '''import UIKit
import WebKit
enum Links {
    static let privacy = URL(string: "https://not-deployed.invalid/privacy")!
    static let terms = URL(string: "https://not-deployed.invalid/support")!
}
final class DocumentPane: UIViewController {
    private let address: URL
    private let heading: String
    private var browser: WKWebView!
    init(url: URL, heading: String) {
        self.address = url
        self.heading = heading
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError("unsupported") }
    override func viewDidLoad() {
        super.viewDidLoad()
        title = heading
        let modalRoot = navigationController?.viewControllers.first === self && navigationController?.presentingViewController != nil
        if modalRoot {
            navigationItem.leftBarButtonItem = UIBarButtonItem(title: "Close", style: .plain, target: self, action: #selector(closePage))
        }
        browser = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        view.addSubview(browser)
        reloadPage()
    }
    @objc private func closePage() { dismiss(animated: true) }
    @objc private func reloadPage() {
        browser.isHidden = false
        browser.load(URLRequest(url: address, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 30))
    }
}
''',
    "Gate.swift": '''import UIKit
final class GatePane: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        let privacy = UIButton(type: .system)
        privacy.setTitle("Privacy Policy", for: .normal)
        privacy.addTarget(self, action: #selector(openPrivacy), for: .touchUpInside)
        view.addSubview(privacy)
        let support = UIButton(type: .system)
        support.setTitle("Support", for: .normal)
        support.addTarget(self, action: #selector(openSupport), for: .touchUpInside)
        view.addSubview(support)
    }
    @objc private func openPrivacy() {
        let pane = DocumentPane(url: Links.privacy, heading: "Privacy Policy")
        let nav = UINavigationController(rootViewController: pane)
        nav.modalPresentationStyle = .fullScreen
        present(nav, animated: true)
    }
    @objc private func openSupport() {
        let pane = DocumentPane(url: Links.terms, heading: "Terms & Support")
        let nav = UINavigationController(rootViewController: pane)
        present(nav, animated: true)
    }
}
''',
    "Profile.swift": '''import UIKit
final class ProfilePane: UIViewController, UITableViewDataSource, UITableViewDelegate {
    private enum Section: Int { case account, legal }
    func tableView(_ tableView: UITableView, cellForRowAt indexPath: IndexPath) -> UITableViewCell {
        let cell = UITableViewCell()
        switch Section(rawValue: indexPath.section)! {
        case .account:
            cell.textLabel?.text = "Account"
        case .legal:
            cell.textLabel?.text = indexPath.row == 0 ? "Privacy Policy" : "Terms & Support"
        }
        return cell
    }
    func tableView(_ tableView: UITableView, didSelectRowAt indexPath: IndexPath) {
        switch Section(rawValue: indexPath.section)! {
        case .account:
            break
        case .legal:
            if indexPath.row == 0 {
                navigationController?.pushViewController(DocumentPane(url: Links.privacy, heading: "Privacy Policy"), animated: true)
            } else {
                navigationController?.pushViewController(DocumentPane(url: Links.terms, heading: "Terms & Support"), animated: true)
            }
        }
    }
}
''',
    "Locker.swift": '''import UIKit
import SafariServices
final class LockerPane: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        let column = UIStackView()
        column.addArrangedSubview(makeLink("Privacy Policy", id: "privacy"))
        column.addArrangedSubview(makeLink("Terms & Support", id: "support"))
        view.addSubview(column)
    }
    private func makeLink(_ title: String, id: String) -> UIButton {
        let button = UIButton(type: .system)
        button.setTitle(title, for: .normal)
        button.accessibilityIdentifier = id
        button.addTarget(self, action: #selector(openLink(_:)), for: .touchUpInside)
        return button
    }
    @objc private func openLink(_ sender: UIButton) {
        switch sender.accessibilityIdentifier {
        case "privacy":
            navigationController?.pushViewController(DocumentPane(url: Links.privacy, heading: "Privacy Policy"), animated: true)
        case "support":
            navigationController?.pushViewController(DocumentPane(url: Links.terms, heading: "Terms & Support"), animated: true)
        default:
            break
        }
    }
}
''',
}


def write_associated_legal_fixture(root: Path) -> None:
    for name, source in SOURCES.items():
        (root / name).write_text(source)
