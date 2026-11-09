/* Hostile probe helper: can this process open an IOKit user client? exit 0 = opened.
   IOSurfaceRoot is openable by any ordinary process on macOS (graphics clients use it). */
#include <IOKit/IOKitLib.h>
int main(void) {
    io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOSurfaceRoot"));
    if (!svc) return 2;
    io_connect_t conn = 0;
    kern_return_t kr = IOServiceOpen(svc, mach_task_self(), 0, &conn);
    IOObjectRelease(svc);
    if (kr != KERN_SUCCESS) return 1;
    IOServiceClose(conn);
    return 0;
}
