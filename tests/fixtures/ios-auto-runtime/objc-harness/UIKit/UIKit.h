#import <Foundation/Foundation.h>

@interface UIEvent : NSObject
@end

@interface UIApplication : NSObject
+ (instancetype)sharedApplication;
- (BOOL)sendAction:(SEL)action
                to:(id)target
              from:(id)sender
          forEvent:(UIEvent *)event;
@end
