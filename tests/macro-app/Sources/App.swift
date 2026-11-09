import Observation
import SwiftUI

// The Debug configuration sets OTHER_SWIFT_FLAGS = -DBH_PROJECT_FLAG (no $(inherited)). The runner
// must not replace the project's own Swift flags.
#if DEBUG && !BH_PROJECT_FLAG
#error("the project's own OTHER_SWIFT_FLAGS did not reach the compiler")
#endif

@main
struct MacroApp: App {
    var body: some Scene {
        WindowGroup { Counter() }
    }
}

/// @Observable: ObservationMacros.ObservableMacro (swift-plugin-server).
@Observable
final class Model {
    var taps = 0
}

/// @State: SwiftUIMacros.StateMacro in the Xcode 27 SDK.
struct Counter: View {
    @State private var count = 0
    @State private var model = Model()

    var body: some View {
        Button("Count \(count), taps \(model.taps)") {
            count += 1
            model.taps += 1
        }
    }
}

/// #Preview: PreviewsMacros.SwiftUIView.
#Preview {
    Counter()
}
