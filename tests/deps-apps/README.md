Remote-dependency fixtures for tests/run-tests.sh (generated with xcodegen; the .xcodeproj is committed):
- local-pkg: depends on a vendored local-path package (XCLocalSwiftPackageReference). Must build.
- remote-pbx: XCRemoteSwiftPackageReference in project.pbxproj. Must be refused with unsupported_dependencies.
- Package.resolved.remote: dropped into the sample app's project.xcworkspace/xcshareddata/swiftpm/ by the test.
