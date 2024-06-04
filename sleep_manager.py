from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
import platform
import ctypes

mac_assertion_id = None
linux_cookie = None


def create_cfstring(string):
    """Helper function to create a CFStringRef"""
    CoreFoundation = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    CoreFoundation.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    CoreFoundation.CFStringCreateWithCString.restype = ctypes.c_void_p
    kCFStringEncodingUTF8 = 0x08000100
    return CoreFoundation.CFStringCreateWithCString(
        None, string.encode("utf-8"), kCFStringEncodingUTF8
    )


def prevent_sleep():
    if platform.system() == "Windows":
        prevent_sleep_windows()
    elif platform.system() == "Darwin":
        prevent_sleep_macos()
    elif platform.system() == "Linux":
        prevent_sleep_linux()


def allow_sleep():
    if platform.system() == "Windows":
        allow_sleep_windows()
    elif platform.system() == "Darwin":
        allow_sleep_macos()
    elif platform.system() == "Linux":
        allow_sleep_linux()


def prevent_sleep_windows():
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def allow_sleep_windows():
    ES_CONTINUOUS = 0x80000000
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def prevent_sleep_macos():
    global mac_assertion_id
    IOKit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")

    # Define CFSTR constants
    kIOPMAssertionTypeNoDisplaySleep = create_cfstring("NoDisplaySleepAssertion")
    reasonForActivity = create_cfstring("QiTV video playback")

    # Function signature
    IOKit.IOPMAssertionCreateWithName.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    IOKit.IOPMAssertionCreateWithName.restype = ctypes.c_uint32

    # Constants
    kIOPMAssertionLevelOn = 255
    assertionID = ctypes.c_uint32(0)

    # Create assertion
    success = IOKit.IOPMAssertionCreateWithName(
        kIOPMAssertionTypeNoDisplaySleep,
        kIOPMAssertionLevelOn,
        reasonForActivity,
        ctypes.byref(assertionID),
    )

    if success == 0:  # kIOReturnSuccess == 0
        mac_assertion_id = assertionID.value


def allow_sleep_macos():
    global mac_assertion_id
    if mac_assertion_id is not None:
        IOKit = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/IOKit.framework/IOKit"
        )
        IOKit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
        IOKit.IOPMAssertionRelease(mac_assertion_id)
        mac_assertion_id = None


def prevent_sleep_linux():
    global linux_cookie
    interface = "org.freedesktop.ScreenSaver"
    object_path = "/org/freedesktop/ScreenSaver"
    service_name = "org.freedesktop.ScreenSaver"
    connection = QDBusConnection.sessionBus()
    screensaver_interface = QDBusInterface(
        service_name, object_path, interface, connection
    )
    if screensaver_interface.isValid():
        reply = screensaver_interface.call("Inhibit", "QiTV", "Playing video")
        try:
            if reply.type() == QDBusMessage.ReplyMessage:
                linux_cookie = reply.arguments()[0]
        except:
            pass


def allow_sleep_linux():
    global linux_cookie
    if linux_cookie:
        interface = "org.freedesktop.ScreenSaver"
        object_path = "/org/freedesktop/ScreenSaver"
        service_name = "org.freedesktop.ScreenSaver"
        connection = QDBusConnection.sessionBus()
        screensaver_interface = QDBusInterface(
            service_name, object_path, interface, connection
        )
        if screensaver_interface.isValid():
            screensaver_interface.call("UnInhibit", linux_cookie)
