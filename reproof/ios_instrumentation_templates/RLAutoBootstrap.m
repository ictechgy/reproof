#if REPRO_AUTO_DEBUG
#import <UIKit/UIKit.h>
#import <objc/message.h>
#import <objc/runtime.h>
#import <unistd.h>

// This file deliberately has no generated Swift-header or product-module
// dependency. The Swift collector is found by its fixed Objective-C runtime
// name and all calls use a small, manually described ABI.
typedef id (*RLWillSendActionIMP)(id, SEL, SEL, id, id, UIEvent *);
typedef void (*RLActionResultIMP)(id, SEL, id);
typedef void (*RLNoArgIMP)(id, SEL);
typedef BOOL (*RLBootstrapIMP)(id, SEL);
typedef void (*RLViewNotifyIMP)(id, SEL, id);
typedef void (*RLSanitationIMP)(id, SEL);

static Class RLRecorderClass(void) {
    return NSClassFromString(@"RLAutomaticRecorder");
}

static void RLApplySanitationBeforeMain(void) {
    BOOL requested = NSProcessInfo.processInfo.environment[@"REPRO_SANITATION_POLICY_DIGEST"] != nil;
    Class cls = NSClassFromString(@"RLSanitationRuntime");
    SEL selector = NSSelectorFromString(@"applyBeforeMain");
    if (!cls || ![cls respondsToSelector:selector]) {
        if (requested) {
            _exit(78);
        }
        return;
    }
    @try {
        ((RLSanitationIMP)objc_msgSend)((id)cls, selector);
    } @catch (__unused NSException *exception) {
        if (requested) {
            _exit(78);
        }
    }
}

static void RLCallCollectorFailure(void) {
    Class cls = RLRecorderClass();
    SEL selector = NSSelectorFromString(@"_rlCollectorFailure");
    if (![cls respondsToSelector:selector]) {
        return;
    }
    @try {
        ((RLNoArgIMP)objc_msgSend)((id)cls, selector);
    } @catch (__unused NSException *exception) {
        // Collector failures never change product behavior.
    }
}

static void RLCallAppLogFailure(void) {
    Class cls = RLRecorderClass();
    SEL selector = NSSelectorFromString(@"_rlAppLogFailure");
    if (![cls respondsToSelector:selector]) {
        return;
    }
    @try {
        ((RLNoArgIMP)objc_msgSend)((id)cls, selector);
    } @catch (__unused NSException *exception) {
        // App-log failures never change product behavior.
    }
}

static id RLWillSendAction(SEL action, id target, id sender, UIEvent *event) {
    Class cls = RLRecorderClass();
    SEL selector = NSSelectorFromString(@"_rlWillSendAction:to:from:forEvent:");
    if (![cls respondsToSelector:selector]) {
        return nil;
    }
    @try {
        return ((RLWillSendActionIMP)objc_msgSend)((id)cls, selector, action, target, sender, event);
    } @catch (__unused NSException *exception) {
        RLCallCollectorFailure();
        return nil;
    }
}

static void RLActionResult(id token, BOOL threw) {
    if (!token) {
        return;
    }
    Class cls = RLRecorderClass();
    SEL selector = NSSelectorFromString(threw ? @"_rlActionThrew:" : @"_rlActionReturned:");
    if (![cls respondsToSelector:selector]) {
        return;
    }
    @try {
        ((RLActionResultIMP)objc_msgSend)((id)cls, selector, token);
    } @catch (__unused NSException *exception) {
        RLCallCollectorFailure();
    }
}

static BOOL (*RLOriginalSendAction)(id, SEL, SEL, id, id, UIEvent *);
static void (*RLOriginalViewDidAppear)(id, SEL, BOOL);
static void (*RLOriginalViewDidDisappear)(id, SEL, BOOL);

static BOOL RLAutomaticSendAction(id self,
                                  SEL command,
                                  SEL action,
                                  id target,
                                  id sender,
                                  UIEvent *event) {
    id token = RLWillSendAction(action, target, sender, event);
    @try {
        // The saved IMP is invoked exactly once and its return value is
        // returned unchanged. It is also the only product dispatch path.
        BOOL result = RLOriginalSendAction(self, command, action, target, sender, event);
        RLActionResult(token, NO);
        return result;
    } @catch (NSException *exception) {
        RLActionResult(token, YES);
        @throw;
    }
}

static void RLNotifyViewLifecycle(id controller, BOOL appeared) {
    Class cls = RLRecorderClass();
    SEL selector = NSSelectorFromString(appeared ? @"_rlViewControllerAppearing:" : @"_rlViewControllerDisappearing:");
    if (![cls respondsToSelector:selector]) {
        return;
    }
    @try {
        ((RLViewNotifyIMP)objc_msgSend)((id)cls, selector, controller);
    } @catch (__unused NSException *exception) {
        RLCallAppLogFailure();
    }
}

static void RLAutomaticViewDidAppear(id self, SEL command, BOOL animated) {
    RLNotifyViewLifecycle(self, YES);
    @try {
        RLOriginalViewDidAppear(self, command, animated);
    } @catch (NSException *exception) {
        @throw;
    }
}

static void RLAutomaticViewDidDisappear(id self, SEL command, BOOL animated) {
    RLNotifyViewLifecycle(self, NO);
    @try {
        RLOriginalViewDidDisappear(self, command, animated);
    } @catch (NSException *exception) {
        @throw;
    }
}

static void RLInstallSendActionHook(void) {
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        UIApplication *application = [UIApplication sharedApplication];
        Class applicationClass = [application class];
        if (!applicationClass) {
            return;
        }
        SEL selector = @selector(sendAction:to:from:forEvent:);
        Method method = class_getInstanceMethod(applicationClass, selector);
        if (!method) {
            return;
        }
        IMP original = method_getImplementation(method);
        if (!original) {
            return;
        }
        RLOriginalSendAction = (BOOL (*)(id, SEL, SEL, id, id, UIEvent *))original;
        const char *types = method_getTypeEncoding(method);
        if (class_addMethod(applicationClass, selector, original, types)) {
            // The method was inherited. Add a concrete override on the actual
            // UIApplication class so the superclass implementation remains
            // untouched, then replace only that new method.
            class_replaceMethod(applicationClass, selector, (IMP)RLAutomaticSendAction, types);
        } else {
            // The concrete application class already owns the selector.
            class_replaceMethod(applicationClass, selector, (IMP)RLAutomaticSendAction, types);
        }
    });
}

static void RLInstallViewControllerHooks(void) {
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        Class controllerClass = NSClassFromString(@"UIViewController");
        if (!controllerClass) {
            return;
        }
        SEL appearSelector = NSSelectorFromString(@"viewDidAppear:");
        SEL disappearSelector = NSSelectorFromString(@"viewDidDisappear:");
        Method appearMethod = class_getInstanceMethod(controllerClass, appearSelector);
        Method disappearMethod = class_getInstanceMethod(controllerClass, disappearSelector);
        if (appearMethod) {
            IMP original = method_getImplementation(appearMethod);
            const char *types = method_getTypeEncoding(appearMethod);
            RLOriginalViewDidAppear = (void (*)(id, SEL, BOOL))original;
            if (class_addMethod(controllerClass, appearSelector, original, types)) {
                class_replaceMethod(controllerClass, appearSelector, (IMP)RLAutomaticViewDidAppear, types);
            } else {
                class_replaceMethod(controllerClass, appearSelector, (IMP)RLAutomaticViewDidAppear, types);
            }
        }
        if (disappearMethod) {
            IMP original = method_getImplementation(disappearMethod);
            const char *types = method_getTypeEncoding(disappearMethod);
            RLOriginalViewDidDisappear = (void (*)(id, SEL, BOOL))original;
            if (class_addMethod(controllerClass, disappearSelector, original, types)) {
                class_replaceMethod(controllerClass, disappearSelector, (IMP)RLAutomaticViewDidDisappear, types);
            } else {
                class_replaceMethod(controllerClass, disappearSelector, (IMP)RLAutomaticViewDidDisappear, types);
            }
        }
    });
}

static void RLBootstrapCollector(void) {
    Class cls = RLRecorderClass();
    SEL selector = NSSelectorFromString(@"bootstrap");
    if (![cls respondsToSelector:selector]) {
        return;
    }
    @try {
        BOOL enabled = ((RLBootstrapIMP)objc_msgSend)((id)cls, selector);
        if (enabled) {
            RLInstallSendActionHook();
            RLInstallViewControllerHooks();
        }
    } @catch (__unused NSException *exception) {
        RLCallCollectorFailure();
    }
}

@interface RLAutoBootstrap : NSObject
@end

@implementation RLAutoBootstrap
+ (void)load {
    RLApplySanitationBeforeMain();
    dispatch_async(dispatch_get_main_queue(), ^{
        RLBootstrapCollector();
    });
}
@end

#endif
