import UIKit

final class CounterViewController: UIViewController, UIScrollViewDelegate, UITextFieldDelegate {
    private let nameField = UITextField()
    private let countLabel = UILabel()
    private let buildIDLabel = UILabel()
    private let statusLabel = UILabel()
    private let listScrollView = UIScrollView()
    private let contentView = UIView()
    private let bottomLabel = UILabel()
    private let mainPanel = UIView()
    private let detailPanel = UIView()
    private var count = 0
    private var name = ""
    private var nameDirty = false
    private var hasRecordedBottom = false
    private var submitted = false

    private let reproCase = ReproCase.current

    private var buildID: String {
        Bundle.main.object(forInfoDictionaryKey: "ReproBuildID") as? String ?? "unknown"
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        view.accessibilityIdentifier = "counter.screen.main"
        configureMainPanel()
        configureDetailPanel()
        updateLabels()
    }

    private func configureMainPanel() {
        mainPanel.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(mainPanel)
        NSLayoutConstraint.activate([
            mainPanel.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 20),
            mainPanel.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -20),
            mainPanel.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 20),
            mainPanel.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -20)
        ])

        let title = UILabel()
        title.text = "Counter"
        title.font = .preferredFont(forTextStyle: .largeTitle)

        nameField.borderStyle = .roundedRect
        nameField.accessibilityLabel = "Name"
        nameField.accessibilityIdentifier = "counter.name"
        nameField.delegate = self
        nameField.returnKeyType = .done
        nameField.addTarget(self, action: #selector(nameEditingChanged), for: .editingChanged)

        let toolbar = UIToolbar()
        toolbar.sizeToFit()
        let done = UIBarButtonItem(title: "Done", style: .done, target: self, action: #selector(dismissKeyboard))
        done.accessibilityIdentifier = "counter.keyboard_accessory_done"
        toolbar.items = [UIBarButtonItem.flexibleSpace(), done]
        nameField.inputAccessoryView = toolbar

        let commit = makeButton(title: "Done", identifier: "counter.keyboard_done", action: #selector(dismissKeyboard))
        let clear = makeButton(title: "Clear", identifier: "counter.clear", action: #selector(clearName))
        let addTitle = reproCase == .duplicateSubmit ? "Submit" : "Add"
        let add = makeButton(title: addTitle, identifier: "counter.add", action: #selector(addCount))
        let reset = makeButton(title: "Reset", identifier: "counter.reset", action: #selector(resetCount))
        let next = makeButton(title: "Next", identifier: "counter.next", action: #selector(showDetails))
        let report = makeButton(title: "Report", identifier: "counter.report", action: #selector(reportCapture))

        countLabel.accessibilityIdentifier = "counter.count"
        countLabel.font = .preferredFont(forTextStyle: .title2)
        countLabel.textAlignment = .center

        buildIDLabel.accessibilityIdentifier = "counter.build_id"
        buildIDLabel.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        buildIDLabel.textColor = .secondaryLabel

        statusLabel.accessibilityIdentifier = "counter.capture_ready"
        statusLabel.isHidden = true
        statusLabel.textColor = .systemGreen

        listScrollView.accessibilityIdentifier = "counter.list"
        listScrollView.delegate = self
        listScrollView.alwaysBounceVertical = true
        listScrollView.translatesAutoresizingMaskIntoConstraints = false
        contentView.translatesAutoresizingMaskIntoConstraints = false
        listScrollView.addSubview(contentView)
        contentView.addSubview(bottomLabel)
        bottomLabel.accessibilityIdentifier = "counter.bottom"
        bottomLabel.text = "End of list"
        bottomLabel.textColor = .secondaryLabel
        bottomLabel.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            contentView.leadingAnchor.constraint(equalTo: listScrollView.contentLayoutGuide.leadingAnchor),
            contentView.trailingAnchor.constraint(equalTo: listScrollView.contentLayoutGuide.trailingAnchor),
            contentView.topAnchor.constraint(equalTo: listScrollView.contentLayoutGuide.topAnchor),
            contentView.bottomAnchor.constraint(equalTo: listScrollView.contentLayoutGuide.bottomAnchor),
            contentView.widthAnchor.constraint(equalTo: listScrollView.frameLayoutGuide.widthAnchor),
            contentView.heightAnchor.constraint(equalToConstant: 760),
            bottomLabel.centerXAnchor.constraint(equalTo: contentView.centerXAnchor),
            bottomLabel.bottomAnchor.constraint(equalTo: contentView.bottomAnchor, constant: -20)
        ])

        var arrangedSubviews: [UIView] = [title, nameField, clear, commit, countLabel, add]
        if reproCase == .reset {
            arrangedSubviews.append(reset)
        }
        arrangedSubviews += [next, report, buildIDLabel, statusLabel, listScrollView]
        let stack = UIStackView(arrangedSubviews: arrangedSubviews)
        stack.axis = .vertical
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        mainPanel.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: mainPanel.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: mainPanel.trailingAnchor),
            stack.topAnchor.constraint(equalTo: mainPanel.topAnchor),
            stack.bottomAnchor.constraint(equalTo: mainPanel.bottomAnchor),
            nameField.heightAnchor.constraint(equalToConstant: 44),
            clear.heightAnchor.constraint(equalToConstant: 40),
            add.heightAnchor.constraint(equalToConstant: 40),
            reset.heightAnchor.constraint(equalToConstant: 40),
            next.heightAnchor.constraint(equalToConstant: 40),
            report.heightAnchor.constraint(equalToConstant: 40),
            listScrollView.heightAnchor.constraint(greaterThanOrEqualToConstant: 140)
        ])
    }

    private func configureDetailPanel() {
        detailPanel.translatesAutoresizingMaskIntoConstraints = false
        detailPanel.isHidden = true
        view.addSubview(detailPanel)
        NSLayoutConstraint.activate([
            detailPanel.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 20),
            detailPanel.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor, constant: -20),
            detailPanel.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 20),
            detailPanel.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -20)
        ])
        let title = UILabel()
        title.text = "Details"
        title.font = .preferredFont(forTextStyle: .largeTitle)
        let copy = UILabel()
        copy.text = "This is a separate screen for replaying navigation."
        copy.numberOfLines = 0
        let back = makeButton(title: "Back", identifier: "counter.back", action: #selector(showMain))
        let stack = UIStackView(arrangedSubviews: [title, copy, back])
        stack.axis = .vertical
        stack.spacing = 16
        stack.translatesAutoresizingMaskIntoConstraints = false
        detailPanel.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: detailPanel.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: detailPanel.trailingAnchor),
            stack.topAnchor.constraint(equalTo: detailPanel.topAnchor),
            back.heightAnchor.constraint(equalToConstant: 44)
        ])
    }

    private func makeButton(title: String, identifier: String, action: Selector) -> UIButton {
        let button = UIButton(type: .system)
        button.setTitle(title, for: .normal)
        button.accessibilityIdentifier = identifier
        button.addTarget(self, action: action, for: .touchUpInside)
        return button
    }

    private func updateLabels() {
        countLabel.text = String(count)
        nameField.text = name
        buildIDLabel.text = "Build: \(buildID)"
    }

    @objc private func nameEditingChanged() {
        name = nameField.text ?? ""
        nameDirty = true
    }

    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        dismissKeyboard()
        return true
    }

    @objc private func dismissKeyboard() {
        flushNameInput()
        view.endEditing(true)
    }

    @objc private func clearName() {
        name = ""
        nameField.text = ""
        nameDirty = true
    }

    private func flushNameInput() {
        guard nameDirty else { return }
        let value = nameField.text ?? name
        name = value
        Recorder.shared.recordReplace(target: "counter.name", value: value)
        nameDirty = false
    }

    @objc private func addCount() {
        flushNameInput()
        switch reproCase {
        case .counter:
            count += CounterLogic.increment()
        case .duplicateSubmit:
            if SubmissionLogic.shouldAccept(submitted: submitted) {
                submitted = true
                count += 1
            }
        case .reset:
            count += 1
        }
        countLabel.text = String(count)
        Recorder.shared.recordTap(target: "counter.add")
    }

    @objc private func resetCount() {
        guard reproCase == .reset else { return }
        flushNameInput()
        count = ResetLogic.resetValue(previous: count)
        countLabel.text = String(count)
        Recorder.shared.recordTap(target: "counter.reset")
    }

    @objc private func showDetails() {
        flushNameInput()
        mainPanel.isHidden = true
        detailPanel.isHidden = false
        view.accessibilityIdentifier = "counter.screen.details"
        Recorder.shared.recordTap(target: "counter.next")
    }

    @objc private func showMain() {
        mainPanel.isHidden = false
        detailPanel.isHidden = true
        view.accessibilityIdentifier = "counter.screen.main"
        Recorder.shared.recordBack(target: "counter.back")
    }

    @objc private func reportCapture() {
        flushNameInput()
        Recorder.shared.finish { [weak self] ready in
            DispatchQueue.main.async {
                guard let self else { return }
                self.statusLabel.isHidden = false
                self.statusLabel.text = ready ? "Capture ready" : "Capture invalid"
            }
        }
    }

    func scrollViewDidScroll(_ scrollView: UIScrollView) {
        guard scrollView === listScrollView, !hasRecordedBottom else { return }
        let bottom = scrollView.contentOffset.y + scrollView.bounds.height
        let targetFrame = bottomLabel.convert(bottomLabel.bounds, to: scrollView)
        let visibleBounds = CGRect(origin: scrollView.contentOffset, size: scrollView.bounds.size)
        if scrollView.contentOffset.y > 0 && targetFrame.intersects(visibleBounds) {
            flushNameInput()
            hasRecordedBottom = true
            Recorder.shared.recordScroll(target: "counter.bottom", container: "counter.list")
        }
    }
}
