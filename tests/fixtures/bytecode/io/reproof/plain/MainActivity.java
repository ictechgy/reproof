package io.reproof.plain;

import android.app.Activity;
import android.os.Bundle;
import android.view.View;

public class MainActivity extends Activity {
    public final View normal = new View();
    public final View labeledReturn = new View();
    public final View throwing = new View();
    public final View unrelated = new View();
    public int normalCalls;
    public int labeledCalls;
    public int throwCalls;
    public int destroyCalls;

    @Override
    public void onCreate(Bundle state) {
        normal.setOnClickListener(view -> normalCalls++);
        labeledReturn.setOnClickListener(view -> {
            if (view == labeledReturn) return;
            labeledCalls++;
        });
        throwing.setOnClickListener(view -> {
            throw new IllegalStateException("fixture");
        });
        unrelated.setOnClickListener(view -> normalCalls += 100);
    }

    public void unrelatedListener() {
        unrelated.setOnClickListener(view -> normalCalls += 10);
    }

    @Override
    public void onDestroy() {
        destroyCalls++;
        super.onDestroy();
    }
}
