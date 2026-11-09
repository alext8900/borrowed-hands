// Using #probe makes swift-frontend launch probe-plugin.sh (see project.yml).
@freestanding(expression)
macro probe() -> Int = #externalMacro(module: "ProbeMacros", type: "ProbeMacro")

print(#probe())
