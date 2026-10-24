// Cocoa/WebKit is the display adapter. PTYs, queues, lifecycle and game state
// live in C23; the bundled Python engine keeps agent behavior and shell themes.
#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#import <QuartzCore/QuartzCore.h>
#include "tether_runtime.h"
#include "tether_orb.h"
#include "tether_files.h"
#include <math.h>
#include <signal.h>
#include <time.h>
#include <libproc.h>

@interface TetherHost : NSObject <NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate> {
    tt_runtime *_runtime;
    tt_orb _orb;
}
@property NSWindow *window;
@property WKWebView *web;
@property NSTimer *timer;
@property NSMutableDictionary<NSNumber *, NSMutableDictionary *> *sessions;
@property NSString *resources;
@property NSString *cwd;
@property NSString *smokePath;
@property BOOL ready;
@property BOOL stopping;
@property BOOL deliveredOrbActive;
@property BOOL orbPending;
@property CFTimeInterval lastTick;
@property CFTimeInterval lastProgramPoll;
@property CGPoint testMousePosition;
@property BOOL testMovedMouse;
@property dispatch_queue_t fileQueue;
@property NSUInteger pendingFiles;
- (void)deliver:(NSDictionary *)event;
- (void)output:(tt_session_id)sid bytes:(const unsigned char *)bytes length:(size_t)length;
- (void)exited:(tt_session_id)sid code:(int)code;
@end

static void on_output(void *context, tt_session_id sid, const unsigned char *bytes, size_t count) {
    [(__bridge TetherHost *)context output:sid bytes:bytes length:count];
}
static void on_exit(void *context, tt_session_id sid, int code) {
    [(__bridge TetherHost *)context exited:sid code:code];
}

@implementation TetherHost
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    (void)notification;
    self.sessions = [NSMutableDictionary dictionary];
    self.fileQueue = dispatch_queue_create("tether.files", DISPATCH_QUEUE_SERIAL);
    self.resources = NSBundle.mainBundle.resourcePath;
    self.cwd = NSHomeDirectory();
    NSString *stored = [NSUserDefaults.standardUserDefaults stringForKey:@"terminalDirectory"];
    BOOL directory = NO;
    if (stored && [NSFileManager.defaultManager fileExistsAtPath:stored isDirectory:&directory] && directory) self.cwd = stored;
    self.smokePath = NSProcessInfo.processInfo.environment[@"TETHER_APP_SMOKE_REPORT"];
    if (self.smokePath) self.cwd = self.smokePath.stringByDeletingLastPathComponent;
    _runtime = tt_runtime_create(NSBundle.mainBundle.executablePath.fileSystemRepresentation);
    if (!_runtime) { fprintf(stderr, "Cannot initialize terminal runtime\n"); [NSApp terminate:nil]; return; }
    tt_orb_init(&_orb);
    _orb.reduced_motion = NSWorkspace.sharedWorkspace.accessibilityDisplayShouldReduceMotion;
    _orb.enabled = ![NSUserDefaults.standardUserDefaults boolForKey:@"disableThinkingLight"];
    self.window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 1180, 780)
        styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
        backing:NSBackingStoreBuffered defer:NO];
    self.window.title = @"Tether";
    self.window.minSize = NSMakeSize(680, 440);
    self.window.delegate = self;
    self.window.appearance = [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua];
    self.window.backgroundColor = [NSColor colorWithSRGBRed:.047 green:.063 blue:.078 alpha:1];
    self.window.tabbingMode = NSWindowTabbingModeDisallowed;
    WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
    if (self.smokePath) configuration.websiteDataStore = [WKWebsiteDataStore nonPersistentDataStore];
    [configuration.userContentController addScriptMessageHandler:self name:@"tether"];
    self.web = [[WKWebView alloc] initWithFrame:self.window.contentView.bounds configuration:configuration];
    self.web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.web.navigationDelegate = self;
    [self.web setValue:@NO forKey:@"drawsBackground"];
    self.window.contentView = self.web;
    if (self.smokePath && [NSProcessInfo.processInfo.environment[@"TETHER_APP_SMOKE_SMALL"] isEqual:@"1"]) {
        // Keep the test viewport fixed even if macOS restores/tiles the window.
        NSView *fixture = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 680, 440)];
        self.window.contentView = fixture;
        self.web.autoresizingMask = NSViewNotSizable;
        self.web.frame = NSMakeRect(0, 0, 680, 440);
        [fixture addSubview:self.web];
        [self.window setContentSize:NSMakeSize(680, 440)];
    }
    [self installMenu];
    NSURL *url = [NSURL fileURLWithPath:[self.resources stringByAppendingPathComponent:@"web/index.html"]];
    [self.web loadFileURL:url allowingReadAccessToURL:[url URLByDeletingLastPathComponent]];
    [self.window center]; [self.window makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
    self.lastTick = CACurrentMediaTime();
    self.timer = [NSTimer timerWithTimeInterval:1.0/60 target:self selector:@selector(tick:) userInfo:nil repeats:YES];
    [NSRunLoop.mainRunLoop addTimer:self.timer forMode:NSRunLoopCommonModes];
}
- (void)installMenu {
    NSMenu *menu = [NSMenu new];
    NSMenuItem *app = [NSMenuItem new]; [menu addItem:app];
    app.submenu = [[NSMenu alloc] initWithTitle:@"Tether"];
    [app.submenu addItemWithTitle:@"About Tether" action:@selector(orderFrontStandardAboutPanel:) keyEquivalent:@""];
    NSMenuItem *settings = [app.submenu addItemWithTitle:@"Settings…" action:@selector(settingsMenu:) keyEquivalent:@","]; settings.target = self;
    [app.submenu addItem:NSMenuItem.separatorItem];
    [app.submenu addItemWithTitle:@"Hide Tether" action:@selector(hide:) keyEquivalent:@"h"];
    [app.submenu addItemWithTitle:@"Quit Tether" action:@selector(terminate:) keyEquivalent:@"q"];
    NSMenuItem *shell = [NSMenuItem new]; [menu addItem:shell]; shell.submenu = [[NSMenu alloc] initWithTitle:@"Shell"];
    for (NSArray *entry in @[@[@"New Terminal", @"new", @"t"], @[@"Split Right", @"split", @"d"],
                             @[@"Agent", @"agent", @""], @[@"btop Monitor", @"monitor", @""], @[@"Close Pane", @"close", @"w"]]) {
        NSMenuItem *item = [shell.submenu addItemWithTitle:entry[0] action:@selector(shellMenu:) keyEquivalent:entry[2]];
        item.target = self; item.representedObject = entry[1];
    }
    NSMenuItem *edit = [NSMenuItem new]; [menu addItem:edit]; edit.submenu = [[NSMenu alloc] initWithTitle:@"Edit"];
    [edit.submenu addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
    [edit.submenu addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
    [edit.submenu addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
    NSMenuItem *view = [NSMenuItem new]; [menu addItem:view]; view.submenu = [[NSMenu alloc] initWithTitle:@"View"];
    NSMenuItem *files = [view.submenu addItemWithTitle:@"Toggle Explorer" action:@selector(shellMenu:) keyEquivalent:@"b"];
    files.target = self; files.representedObject = @"files";
    NSApp.mainMenu = menu;
}
- (void)settingsMenu:(id)sender { (void)sender; [self deliver:@{@"type":@"menu", @"action":@"settings"}]; }
- (void)shellMenu:(NSMenuItem *)sender { [self deliver:@{@"type":@"menu", @"action":sender.representedObject}]; }
- (void)deliver:(NSDictionary *)event {
    if (!self.ready || self.stopping) return;
    NSData *data = [NSJSONSerialization dataWithJSONObject:event options:0 error:NULL];
    if (!data) return;
    NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    BOOL isOrb = [event[@"type"] isEqual:@"orb"];
    if (isOrb) self.orbPending = YES;
    [self.web evaluateJavaScript:[NSString stringWithFormat:@"window.tetherReceive(%@)", json] completionHandler:^(id result, NSError *error) {
        (void)result; (void)error;
        if (isOrb) self.orbPending = NO;
    }];
}
- (void)reply:(id)request result:(id)result error:(NSString *)error {
    [self deliver:@{@"type":@"reply", @"request":request ?: @0, @"ok":@(error == nil),
                   @"result":result ?: NSNull.null, @"error":error ?: @""}];
}
- (void)readFiles:(NSDictionary *)body request:(id)request {
    NSString *path = [body[@"path"] isKindOfClass:NSString.class] ? body[@"path"] : nil;
    NSString *root = [self.cwd stringByResolvingSymlinksInPath];
    if (!path || [path lengthOfBytesUsingEncoding:NSUTF8StringEncoding] >= TT_FILE_PATH ||
        [path rangeOfString:[NSString stringWithFormat:@"%C", (unichar)0]].location != NSNotFound ||
        ![body[@"root"] isEqual:self.cwd] || self.pendingFiles >= 8) {
        [self reply:request result:nil error:@"Folder changed or file browser is busy. Refresh and try again."]; return;
    }
    BOOL hidden = [body[@"hidden"] isKindOfClass:NSNumber.class] && [body[@"hidden"] boolValue];
    ++self.pendingFiles;
    dispatch_async(self.fileQueue, ^{
        tt_file_result *file = calloc(1, sizeof(*file));
        int code = file ? tt_files_read(root.fileSystemRepresentation, path.length ? path.fileSystemRepresentation : "", hidden, file) : ENOMEM;
        NSString *error = nil;
        NSMutableDictionary *result = [NSMutableDictionary dictionary];
        if (code) error = @"Cannot open this path. It may be unavailable, a symbolic link, or outside the selected folder.";
        else if (file->kind == TT_FILE_DIRECTORY) {
            NSMutableArray *entries = [NSMutableArray array];
            for (size_t i = 0; i < file->count; ++i) {
                NSString *name = [NSString stringWithUTF8String:file->entries[i].name];
                if (!name) continue;
                [entries addObject:@{@"name":name, @"directory":@(file->entries[i].kind == TT_FILE_DIRECTORY),
                                    @"symlink":@(file->entries[i].kind == TT_FILE_LINK)}];
            }
            [result addEntriesFromDictionary:@{@"kind":@"directory", @"entries":entries, @"truncated":@(file->truncated)}];
        } else {
            NSString *content = file->kind == TT_FILE_TEXT ? [[NSString alloc] initWithBytes:file->data length:file->length encoding:NSUTF8StringEncoding] : nil;
            // A byte cap can split the final UTF-8 codepoint. Trim at most 3 bytes.
            for (size_t trim = 1; !content && file->kind == TT_FILE_TEXT && file->truncated && trim <= 3 && trim <= file->length; ++trim)
                content = [[NSString alloc] initWithBytes:file->data length:file->length - trim encoding:NSUTF8StringEncoding];
            [result addEntriesFromDictionary:@{@"kind":content ? @"file" : @"binary", @"content":content ?: @"",
                                              @"size":@(file->size), @"truncated":@(file->truncated)}];
        }
        free(file);
        dispatch_async(dispatch_get_main_queue(), ^{
            --self.pendingFiles;
            [self reply:request result:result error:error];
        });
    });
}
- (tt_session_id)spawn:(NSArray<NSString *> *)arguments cwd:(NSString *)cwd pty:(BOOL)pty defer:(BOOL)defer error:(NSString **)error {
    NSMutableDictionary *environment = [NSProcessInfo.processInfo.environment mutableCopy];
    NSString *bin = [self.resources stringByAppendingPathComponent:@"bin"];
    environment[@"PATH"] = [NSString stringWithFormat:@"%@:%@/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:%@", bin, NSHomeDirectory(), environment[@"PATH"] ?: @""];
    environment[@"TERM"] = @"xterm-256color";
    environment[@"COLORTERM"] = @"truecolor";
    environment[@"TETHER_NATIVE_ACTIVITY"] = @"1";
    environment[@"TETHER_BUNDLED_BIN"] = bin;
    environment[@"PYINSTALLER_RESET_ENVIRONMENT"] = @"1";
    if (!environment[@"LANG"]) environment[@"LANG"] = @"en_US.UTF-8";
    NSMutableArray<NSString *> *envStrings = [NSMutableArray array];
    for (NSString *key in environment) [envStrings addObject:[NSString stringWithFormat:@"%@=%@", key, environment[key]]];
    const char **argv = calloc(arguments.count + 1, sizeof(*argv));
    const char **envp = calloc(envStrings.count + 1, sizeof(*envp));
    if (!argv || !envp) { free(argv); free(envp); *error = @"Not enough memory to open a terminal."; return 0; }
    for (NSUInteger i = 0; i < arguments.count; ++i) argv[i] = arguments[i].UTF8String;
    for (NSUInteger i = 0; i < envStrings.count; ++i) envp[i] = envStrings[i].UTF8String;
    tt_launch launch = {.argv=argv, .envp=envp, .cwd=cwd.fileSystemRepresentation, .cols=100, .rows=30, .pty=pty, .defer_tty=defer};
    char message[512] = {0};
    tt_session_id sid = tt_runtime_spawn(_runtime, &launch, message, sizeof(message));
    free(argv); free(envp);
    if (!sid) *error = [NSString stringWithUTF8String:message] ?: @"Cannot open terminal.";
    return sid;
}
- (void)output:(tt_session_id)sid bytes:(const unsigned char *)bytes length:(size_t)length {
    NSMutableDictionary *session = self.sessions[@(sid)];
    if (!session) { tt_runtime_ack(_runtime, sid, length); return; }
    if (session[@"job"]) {
        NSMutableData *buffer = session[@"buffer"];
        if (buffer.length + length <= 1024 * 1024) [buffer appendBytes:bytes length:length];
        else { session[@"overflow"] = @YES; tt_runtime_request_close(_runtime, sid); }
        tt_runtime_ack(_runtime, sid, length);
    } else {
        NSString *encoded = [[NSData dataWithBytes:bytes length:length] base64EncodedStringWithOptions:0];
        [self deliver:@{@"type":@"output", @"id":@(sid), @"data":encoded}];
    }
}
- (void)exited:(tt_session_id)sid code:(int)code {
    NSMutableDictionary *session = self.sessions[@(sid)];
    tt_orb_remove(&_orb, sid);
    if (session[@"job"]) {
        id parsed = [NSJSONSerialization JSONObjectWithData:session[@"buffer"] options:0 error:NULL];
        NSDictionary *result = [parsed isKindOfClass:NSDictionary.class] ? parsed : nil;
        NSString *error = session[@"overflow"] ? @"Settings response exceeded its limit." : (code ? result[@"error"] ?: @"Could not load shell settings." : nil);
        if (![result isKindOfClass:NSDictionary.class]) { result = nil; error = @"Settings helper returned an invalid response."; }
        [self reply:session[@"request"] result:result error:error];
    } else [self deliver:@{@"type":@"exit", @"id":@(sid), @"code":@(code)}];
    [self.sessions removeObjectForKey:@(sid)];
}
- (void)tick:(NSTimer *)timer {
    (void)timer;
    if (self.stopping) return;
    for (NSNumber *number in self.sessions.allKeys) {
        NSDictionary *job = self.sessions[number];
        if (job[@"started"] && CACurrentMediaTime() - [job[@"started"] doubleValue] > 20)
            tt_runtime_request_close(_runtime, number.unsignedLongLongValue);
    }
    tt_runtime_pump(_runtime, on_output, on_exit, (__bridge void *)self);
    CFTimeInterval now = CACurrentMediaTime();
    if (now - self.lastProgramPoll >= .5) {
        self.lastProgramPoll = now;
        for (NSNumber *number in self.sessions.allKeys) {
            NSMutableDictionary *session = self.sessions[number];
            if (![session[@"kind"] isEqual:@"shell"]) continue;
            pid_t foreground = tt_runtime_foreground_pid(_runtime, number.unsignedLongLongValue);
            char name[128] = {0};
            if (foreground > 0) (void)proc_name(foreground, name, sizeof(name));
            NSString *process = [NSString stringWithUTF8String:name] ?: @"";
            NSString *program = [process isEqual:@"claude"] ? @"claude" :
                ([process isEqual:@"codex"] || [process hasPrefix:@"codex-"]) ? @"codex" : @"";
            if (![session[@"program"] isEqual:program]) {
                session[@"program"] = program;
                [self deliver:@{@"type":@"program", @"id":number, @"program":program}];
            }
        }
    }
    tt_orb_step(&_orb, now - self.lastTick); self.lastTick = now;
    if ((_orb.active || self.deliveredOrbActive) && !self.orbPending) {
        [self deliver:@{@"type":@"orb", @"active":@(_orb.active), @"x":@(_orb.x), @"y":@(_orb.y), @"pulse":@(_orb.pulse), @"score":@(_orb.score), @"combo":@(_orb.combo), @"phase":_orb.phase == TT_REASONING ? @"Reasoning" : @"Thinking"}];
        self.deliveredOrbActive = _orb.active;
    }
}
- (void)userContentController:(WKUserContentController *)controller didReceiveScriptMessage:(WKScriptMessage *)message {
    (void)controller;
    if (!message.frameInfo.mainFrame || ![message.body isKindOfClass:NSDictionary.class]) return;
    NSDictionary *body = message.body;
    NSString *type = body[@"type"];
    if (![type isKindOfClass:NSString.class]) return;
    id request = [body[@"request"] isKindOfClass:NSNumber.class] ? body[@"request"] : @0;
    NSNumber *number = [body[@"id"] isKindOfClass:NSNumber.class] ? body[@"id"] : @0;
    tt_session_id sid = number.unsignedLongLongValue;
    NSString *engine = [self.resources stringByAppendingPathComponent:@"bin/tether"];
    if ([type isEqualToString:@"ready"]) {
        self.ready = YES;
        if (self.smokePath && [NSProcessInfo.processInfo.environment[@"TETHER_APP_SMOKE_SMALL"] isEqual:@"1"])
            [self.window setContentSize:NSMakeSize(680, 440)];
        NSMutableArray *fonts = [NSMutableArray arrayWithObjects:@"Tether Mono", @"Menlo", @"Monaco", nil];
        for (NSString *font in NSFontManager.sharedFontManager.availableFontFamilies)
            if ([font localizedCaseInsensitiveContainsString:@"mono"] || [font localizedCaseInsensitiveContainsString:@"nerd"])
                if (![fonts containsObject:font] && fonts.count < 80) [fonts addObject:font];
        [self deliver:@{@"type":@"bootstrap", @"cwd":self.cwd, @"fonts":fonts, @"orbEnabled":@(_orb.enabled), @"reducedMotion":@(_orb.reduced_motion), @"smoke":@(self.smokePath != nil)}];
    } else if ([type isEqualToString:@"open"]) {
        NSString *kind = body[@"kind"];
        if (![@[@"shell", @"agent", @"monitor"] containsObject:kind]) { [self reply:request result:nil error:@"Unknown terminal type."]; return; }
        NSMutableArray *args;
        if ([kind isEqualToString:@"monitor"]) args = [NSMutableArray arrayWithObject:[self.resources stringByAppendingPathComponent:@"bin/btop"]];
        else args = [NSMutableArray arrayWithArray:@[engine, @"native-app", kind, @"--cwd", self.cwd]];
        NSString *theme = [body[@"theme"] isKindOfClass:NSString.class] ? body[@"theme"] : nil;
        if (theme.length && [kind isEqualToString:@"shell"] && theme.length < 1024) [args addObjectsFromArray:@[@"--theme", theme]];
        NSString *error = nil;
        tt_session_id opened = [self spawn:args cwd:self.cwd pty:YES defer:![kind isEqualToString:@"monitor"] error:&error];
        if (opened) self.sessions[@(opened)] = [@{@"kind":kind} mutableCopy];
        [self reply:request result:@{@"id":@(opened), @"cwd":self.cwd} error:error];
    } else if ([type isEqualToString:@"input"]) {
        NSString *input = [body[@"data"] isKindOfClass:NSString.class] ? body[@"data"] : @"";
        NSData *data = [input dataUsingEncoding:NSUTF8StringEncoding];
        if (!tt_runtime_write(_runtime, sid, data.bytes, data.length)) [self deliver:@{@"type":@"notice", @"message":@"Terminal input is busy; try a smaller paste."}];
    } else if ([type isEqualToString:@"resize"]) {
        if ([body[@"cols"] isKindOfClass:NSNumber.class] && [body[@"rows"] isKindOfClass:NSNumber.class])
            (void)tt_runtime_resize(_runtime, sid, [body[@"cols"] unsignedIntValue], [body[@"rows"] unsignedIntValue]);
    } else if ([type isEqualToString:@"ack"]) {
        if ([body[@"bytes"] isKindOfClass:NSNumber.class]) tt_runtime_ack(_runtime, sid, [body[@"bytes"] unsignedLongLongValue]);
    } else if ([type isEqualToString:@"close"]) { tt_orb_remove(&_orb, sid); tt_runtime_request_close(_runtime, sid);
    } else if ([type isEqualToString:@"activity"]) {
        if ([self.sessions[number][@"kind"] isEqual:@"agent"] && [body[@"sequence"] isKindOfClass:NSNumber.class] && [body[@"phase"] isKindOfClass:NSString.class])
            (void)tt_orb_activity(&_orb, sid, [body[@"sequence"] unsignedLongLongValue], [body[@"phase"] UTF8String]);
    } else if ([type isEqualToString:@"tap"]) {
        if ([body[@"x"] isKindOfClass:NSNumber.class] && [body[@"y"] isKindOfClass:NSNumber.class]) (void)tt_orb_tap(&_orb, [body[@"x"] doubleValue], [body[@"y"] doubleValue]);
    } else if ([type isEqualToString:@"appearance"]) {
        self.window.appearance = [NSAppearance appearanceNamed:[body[@"light"] boolValue] ? NSAppearanceNameAqua : NSAppearanceNameDarkAqua];
        NSString *hex = [body[@"background"] isKindOfClass:NSString.class] ? body[@"background"] : @"";
        unsigned int rgb = 0;
        if (hex.length == 7 && [hex hasPrefix:@"#"] && [[NSScanner scannerWithString:[hex substringFromIndex:1]] scanHexInt:&rgb])
            self.window.backgroundColor = [NSColor colorWithSRGBRed:((rgb >> 16) & 255)/255.0 green:((rgb >> 8) & 255)/255.0 blue:(rgb & 255)/255.0 alpha:1];
    } else if ([type isEqualToString:@"orbPreference"]) {
        _orb.enabled = [body[@"enabled"] boolValue];
        if (!self.smokePath) [NSUserDefaults.standardUserDefaults setBool:!_orb.enabled forKey:@"disableThinkingLight"];
    } else if ([type isEqualToString:@"settings"] || [type isEqualToString:@"saveSettings"]) {
        BOOL save = [type isEqualToString:@"saveSettings"];
        NSData *payload = save && [body[@"settings"] isKindOfClass:NSDictionary.class] ? [NSJSONSerialization dataWithJSONObject:body[@"settings"] options:0 error:NULL] : nil;
        if (save && (!payload || payload.length > 16000)) { [self reply:request result:nil error:@"Invalid settings."]; return; }
        NSString *error = nil;
        tt_session_id job = [self spawn:@[engine, @"native-app", save ? @"save-settings" : @"settings"] cwd:self.cwd pty:NO defer:NO error:&error];
        if (job) {
            self.sessions[@(job)] = [@{@"job":@YES, @"request":request, @"buffer":[NSMutableData data], @"started":@(CACurrentMediaTime())} mutableCopy];
            if (save) { NSMutableData *line = [payload mutableCopy]; [line appendBytes:"\n" length:1]; (void)tt_runtime_write(_runtime, job, line.bytes, line.length); }
        } else [self reply:request result:nil error:error];
    } else if ([type isEqualToString:@"files"]) {
        [self readFiles:body request:request];
    } else if ([type isEqualToString:@"folder"]) {
        NSOpenPanel *panel = NSOpenPanel.openPanel; panel.canChooseDirectories = YES; panel.canChooseFiles = NO; panel.allowsMultipleSelection = NO;
        panel.directoryURL = [NSURL fileURLWithPath:self.cwd];
        [panel beginSheetModalForWindow:self.window completionHandler:^(NSModalResponse response) {
            if (response == NSModalResponseOK) { self.cwd = panel.URL.path; [NSUserDefaults.standardUserDefaults setObject:self.cwd forKey:@"terminalDirectory"]; }
            [self reply:request result:self.cwd error:NULL];
        }];
    } else if (([type isEqualToString:@"testDrag"] || [type isEqualToString:@"testRightClick"] || [type isEqualToString:@"testHover"]) && self.smokePath) {
        // Native input regression only: route mouse events through Cocoa and
        // WebKit, including hit testing and pointer capture (never DOM stubs).
        BOOL rightClick = [type isEqualToString:@"testRightClick"];
        BOOL hover = [type isEqualToString:@"testHover"];
        NSArray *keys = rightClick || hover ? @[@"x", @"y"] : @[@"x", @"y", @"endX", @"endY"];
        for (NSString *key in keys) {
            if (![body[key] isKindOfClass:NSNumber.class]) { [self reply:request result:nil error:@"Invalid drag coordinate."]; return; }
            double value = [body[key] doubleValue];
            double limit = [key hasSuffix:@"X"] || [key isEqual:@"x"] ? self.web.bounds.size.width : self.web.bounds.size.height;
            if (!isfinite(value) || value < 0 || value > limit) { [self reply:request result:nil error:@"Drag outside test window."]; return; }
        }
        for (NSInteger step = 0; step <= (hover ? 0 : rightClick ? 1 : 13); ++step) {
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(step * 0.025 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
                double fraction = rightClick || hover ? 0 : MIN(1.0, (double)step / 12.0);
                double x = [body[@"x"] doubleValue] + ([body[@"endX"] doubleValue] - [body[@"x"] doubleValue]) * fraction;
                double y = [body[@"y"] doubleValue] + ([body[@"endY"] doubleValue] - [body[@"y"] doubleValue]) * fraction;
                NSPoint point = [self.web convertPoint:NSMakePoint(x, self.web.isFlipped ? y : self.web.bounds.size.height - y) toView:nil];
                NSEventType eventType = rightClick ? (step == 0 ? NSEventTypeRightMouseDown : NSEventTypeRightMouseUp) : step == 0 ? NSEventTypeLeftMouseDown : step == 13 ? NSEventTypeLeftMouseUp : NSEventTypeLeftMouseDragged;
                if (hover) eventType = NSEventTypeMouseMoved;
                if (hover) {
                    self.window.acceptsMouseMovedEvents = YES;
                    if (!self.testMovedMouse) {
                        CGEventRef current = CGEventCreate(NULL);
                        if (current) { self.testMousePosition = CGEventGetLocation(current); CFRelease(current); self.testMovedMouse = YES; }
                    }
                    NSPoint screen = [self.window convertPointToScreen:point];
                    CGWarpMouseCursorPosition(CGPointMake(screen.x, NSScreen.screens.firstObject.frame.size.height - screen.y));
                }
                NSEvent *event = [NSEvent mouseEventWithType:eventType location:point modifierFlags:0 timestamp:NSProcessInfo.processInfo.systemUptime
                    windowNumber:self.window.windowNumber context:nil eventNumber:step clickCount:1 pressure:step == 13 ? 0 : 1];
                [NSApp postEvent:event atStart:NO];
            });
        }
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.55 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            [self reply:request result:@YES error:nil];
        });
    } else if ([type isEqualToString:@"snapshot"] && self.smokePath) {
        NSString *name = body[@"name"];
        if ([@[@"terminal", @"btop", @"light", @"explorer", @"themes"] containsObject:name]) {
            [self.web takeSnapshotWithConfiguration:nil completionHandler:^(NSImage *image, NSError *error) {
                if (image && !error) {
                    NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithData:image.TIFFRepresentation];
                    NSString *path = [NSString stringWithFormat:@"%@.%@.png", self.smokePath, name];
                    [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:path atomically:YES];
                }
                [self reply:request result:@(image != nil && error == nil) error:nil];
            }];
        }
    } else if ([type isEqualToString:@"smoke"]) {
        // Only enabled by an explicit test environment; no public execution hook.
        if (self.smokePath && [body[@"result"] isKindOfClass:NSDictionary.class]) {
            NSData *data = [NSJSONSerialization dataWithJSONObject:body[@"result"] options:NSJSONWritingPrettyPrinted error:NULL];
            [data writeToFile:self.smokePath atomically:YES];
            [self.web takeSnapshotWithConfiguration:nil completionHandler:^(NSImage *image, NSError *error) {
                if (image && !error) { NSBitmapImageRep *rep = [[NSBitmapImageRep alloc] initWithData:image.TIFFRepresentation];
                    [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:[self.smokePath stringByAppendingString:@".png"] atomically:YES]; }
                [NSApp terminate:nil];
            }];
        }
    }
}
- (void)webView:(WKWebView *)webView decidePolicyForNavigationAction:(WKNavigationAction *)action decisionHandler:(void (^)(WKNavigationActionPolicy))decisionHandler {
    (void)webView;
    // Terminal escape sequences and rendered text cannot navigate into remote pages.
    NSURL *url = action.request.URL;
    NSString *allowed = [self.resources stringByAppendingPathComponent:@"web/"];
    decisionHandler(url.isFileURL && [url.path hasPrefix:allowed] ? WKNavigationActionPolicyAllow : WKNavigationActionPolicyCancel);
}
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender { (void)sender; return YES; }
- (void)applicationWillTerminate:(NSNotification *)notification {
    (void)notification; self.stopping = YES; [self.timer invalidate];
    if (self.testMovedMouse) CGWarpMouseCursorPosition(self.testMousePosition);
    if (_runtime) { tt_runtime_destroy(_runtime); _runtime = NULL; }
    [self.web.configuration.userContentController removeScriptMessageHandlerForName:@"tether"];
}
@end

int main(int argc, char *argv[]) {
    if (argc > 1 && !strcmp(argv[1], "--pty-child")) return tt_runtime_child_main(argc, argv);
    @autoreleasepool {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        TetherHost *host = [TetherHost new]; NSApp.delegate = host;
        [NSApp run];
    }
    return 0;
}
