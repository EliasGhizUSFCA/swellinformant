"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bell, KeyRound, MapPin, Smartphone, Star, Trash2, User as UserIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";

import { AirportPicker } from "@/components/airport-picker";
import { Field } from "@/components/field";
import { AppShell } from "@/components/layout/app-shell";
import { PageHeader } from "@/components/page-header";
import { RequireAuth } from "@/components/require-auth";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { qk } from "@/hooks/queries";
import { del, errorMessage, patch, post } from "@/lib/api";
import type { NotificationPrefsInput, ProfileInput, SavedAirport, User } from "@/types/api";

function useUserUpdate() {
  const queryClient = useQueryClient();
  return (user: User) => queryClient.setQueryData(qk.me, user);
}

function ProfileTab({ user }: { user: User }) {
  const setUser = useUserUpdate();
  const p = user.profile;
  const [form, setForm] = useState({
    full_name: user.full_name,
    home_airport: p.home_airport ? [p.home_airport] : ([] as string[]),
    home_city: p.home_city ?? "",
    preferred_wave_min_ft: p.preferred_wave_min_ft?.toString() ?? "",
    preferred_wave_max_ft: p.preferred_wave_max_ft?.toString() ?? "",
    preferred_break_type: p.preferred_break_type ?? "any",
    experience_level: p.experience_level ?? "any",
    units: p.units,
    currency: p.currency,
  });
  const save = useMutation({
    mutationFn: () => {
      const body: ProfileInput = {
        full_name: form.full_name,
        home_airport: form.home_airport[0] ?? null,
        home_city: form.home_city || null,
        preferred_wave_min_ft: form.preferred_wave_min_ft ? Number(form.preferred_wave_min_ft) : null,
        preferred_wave_max_ft: form.preferred_wave_max_ft ? Number(form.preferred_wave_max_ft) : null,
        preferred_break_type: form.preferred_break_type === "any" ? null : (form.preferred_break_type as ProfileInput["preferred_break_type"]),
        experience_level: form.experience_level === "any" ? null : (form.experience_level as ProfileInput["experience_level"]),
        units: form.units as "ft" | "m",
        currency: form.currency,
      };
      return patch<User>("/api/users/me/profile", body);
    },
    onSuccess: (u) => {
      setUser(u);
      toast.success("Profile saved.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Profile</CardTitle>
        <CardDescription>Used to pre-fill new searches.</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-5 sm:grid-cols-2">
        <Field label="Full name" htmlFor="full_name">
          <Input id="full_name" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
        </Field>
        <Field label="Email" htmlFor="email" hint={user.email_verified ? "Verified" : "Not verified yet"}>
          <Input id="email" value={user.email} disabled />
        </Field>
        <Field label="Home airport" htmlFor="home_airport">
          <AirportPicker id="home_airport" value={form.home_airport} onChange={(v) => setForm({ ...form, home_airport: v.slice(-1) })} max={1} />
        </Field>
        <Field label="Home city" htmlFor="home_city">
          <Input id="home_city" value={form.home_city} onChange={(e) => setForm({ ...form, home_city: e.target.value })} />
        </Field>
        <Field label="Preferred wave size (ft, min)" htmlFor="pmin">
          <Input id="pmin" type="number" value={form.preferred_wave_min_ft} onChange={(e) => setForm({ ...form, preferred_wave_min_ft: e.target.value })} />
        </Field>
        <Field label="Preferred wave size (ft, max)" htmlFor="pmax">
          <Input id="pmax" type="number" value={form.preferred_wave_max_ft} onChange={(e) => setForm({ ...form, preferred_wave_max_ft: e.target.value })} />
        </Field>
        <Field label="Preferred break type" htmlFor="pbreak">
          <Select value={form.preferred_break_type} onValueChange={(v) => setForm({ ...form, preferred_break_type: v })}>
            <SelectTrigger id="pbreak">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {["any", "reef", "point", "beach", "rivermouth", "slab"].map((b) => (
                <SelectItem key={b} value={b} className="capitalize">
                  {b === "any" ? "No preference" : b}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Experience level" htmlFor="exp">
          <Select value={form.experience_level} onValueChange={(v) => setForm({ ...form, experience_level: v })}>
            <SelectTrigger id="exp">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {["any", "beginner", "intermediate", "advanced", "expert"].map((b) => (
                <SelectItem key={b} value={b} className="capitalize">
                  {b === "any" ? "Prefer not to say" : b}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Field>
        <Field label="Height units" htmlFor="units">
          <Select value={form.units} onValueChange={(v) => setForm({ ...form, units: v })}>
            <SelectTrigger id="units">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="ft">Feet</SelectItem>
              <SelectItem value="m">Metres</SelectItem>
            </SelectContent>
          </Select>
        </Field>
        <Field label="Default currency" htmlFor="currency">
          <Input id="currency" value={form.currency} maxLength={3} onChange={(e) => setForm({ ...form, currency: e.target.value.toUpperCase() })} />
        </Field>
        <div className="sm:col-span-2">
          <Button onClick={() => save.mutate()} disabled={save.isPending}>
            Save profile
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function AirportsTab({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<string[]>([]);
  const refresh = (airports: SavedAirport[]) => queryClient.setQueryData(qk.me, { ...user, airports });
  const add = useMutation({
    mutationFn: ({ code, home }: { code: string; home: boolean }) => post<SavedAirport[]>("/api/users/me/airports", { airport_iata: code, is_home: home }),
    onSuccess: (a) => {
      refresh(a);
      setPending([]);
      void queryClient.invalidateQueries({ queryKey: qk.me });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: (id: number) => del<SavedAirport[]>(`/api/users/me/airports/${id}`),
    onSuccess: refresh,
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Saved departure airports</CardTitle>
        <CardDescription>Quick-add these when you create a search.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          {user.airports.length === 0 && <p className="text-sm text-muted-foreground">No saved airports.</p>}
          {user.airports.map((a) => (
            <div key={a.id} className="flex items-center justify-between gap-3 rounded-xl border p-3">
              <div className="flex items-center gap-3">
                <MapPin className="size-4 text-ocean" />
                <div>
                  <div className="font-medium">
                    {a.airport_iata} · {a.city}
                  </div>
                  <div className="text-xs text-muted-foreground">{a.name}</div>
                </div>
                {a.is_home && <Badge variant="good">home</Badge>}
              </div>
              <div className="flex gap-1">
                {!a.is_home && (
                  <Button variant="ghost" size="sm" onClick={() => add.mutate({ code: a.airport_iata, home: true })}>
                    <Star /> Make home
                  </Button>
                )}
                <Button variant="ghost" size="icon" onClick={() => remove.mutate(a.id)} aria-label={`Remove ${a.airport_iata}`}>
                  <Trash2 />
                </Button>
              </div>
            </div>
          ))}
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
          <div className="flex-1">
            <AirportPicker value={pending} onChange={(v) => setPending(v.slice(-1))} max={1} />
          </div>
          <Button disabled={!pending[0] || add.isPending} onClick={() => pending[0] && add.mutate({ code: pending[0], home: user.airports.length === 0 })}>
            Save airport
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function NotificationsTab({ user }: { user: User }) {
  const setUser = useUserUpdate();
  const prefs = user.notification_preferences;
  const [phone, setPhone] = useState(prefs.phone_number ?? "");
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const update = useMutation({
    mutationFn: (body: NotificationPrefsInput) => patch<User>("/api/users/me/notification-preferences", body),
    onSuccess: (u) => {
      setUser(u);
      toast.success("Preferences saved.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const start = useMutation({
    mutationFn: () => post<{ message: string }>("/api/users/me/phone/start", { phone_number: phone }),
    onSuccess: (r) => {
      setCodeSent(true);
      toast.success(r.message);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const confirm = useMutation({
    mutationFn: () => post<User>("/api/users/me/phone/confirm", { code }),
    onSuccess: (u) => {
      setUser(u);
      setCodeSent(false);
      setCode("");
      toast.success("Phone verified — SMS alerts are on.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const optOut = useMutation({
    mutationFn: () => post<User>("/api/users/me/sms/opt-out"),
    onSuccess: (u) => {
      setUser(u);
      toast.success("SMS alerts turned off.");
    },
  });
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Alerts</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <label className="flex items-center justify-between gap-4 rounded-xl border p-4">
            <span>
              <span className="block text-sm font-medium">Pause all notifications</span>
              <span className="text-xs text-muted-foreground">Searches keep running; nothing is sent while paused.</span>
            </span>
            <Switch checked={prefs.all_paused} onCheckedChange={(v) => update.mutate({ all_paused: v })} aria-label="Pause all notifications" />
          </label>
          <label className="flex items-center justify-between gap-4 rounded-xl border p-4">
            <span>
              <span className="block text-sm font-medium">Email alerts</span>
              <span className="text-xs text-muted-foreground">{user.email_verified ? user.email : "Verify your email to receive alerts."}</span>
            </span>
            <Switch checked={prefs.email_enabled} onCheckedChange={(v) => update.mutate({ email_enabled: v })} aria-label="Email alerts" />
          </label>
          <div className="flex items-center justify-between gap-4 rounded-xl border p-4">
            <span>
              <span className="block text-sm font-medium">Maximum alerts per day</span>
              <span className="text-xs text-muted-foreground">Protects your inbox during big swell seasons.</span>
            </span>
            <Select value={String(prefs.max_alerts_per_day)} onValueChange={(v) => update.mutate({ max_alerts_per_day: Number(v) })}>
              <SelectTrigger className="w-24" aria-label="Maximum alerts per day">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {[1, 3, 5, 10, 20].map((n) => (
                  <SelectItem key={n} value={String(n)}>
                    {n}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Smartphone className="size-4 text-ocean" /> SMS alerts
          </CardTitle>
          <CardDescription>Opt-in with a verification code. Reply STOP to any message to opt out.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {!prefs.sms_service_available && (
            <Alert variant="warn">
              <AlertDescription>SMS delivery is not configured on this server.</AlertDescription>
            </Alert>
          )}
          {prefs.sms_ready ? (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <span className="text-sm">
                Sending to <strong>{prefs.phone_number}</strong> <Badge variant="good">verified</Badge>
              </span>
              <Button variant="outline" onClick={() => optOut.mutate()}>
                Turn off SMS
              </Button>
            </div>
          ) : (
            <div className="space-y-3">
              {prefs.sms_opted_out && <p className="text-sm text-muted-foreground">You opted out of SMS. Verify again to opt back in.</p>}
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input placeholder="+14155550123" value={phone} onChange={(e) => setPhone(e.target.value)} aria-label="Phone number" />
                <Button onClick={() => start.mutate()} disabled={start.isPending || !phone || !prefs.sms_service_available}>
                  Send code
                </Button>
              </div>
              {codeSent && (
                <div className="flex flex-col gap-2 sm:flex-row">
                  <Input placeholder="6-digit code" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" aria-label="Verification code" />
                  <Button onClick={() => confirm.mutate()} disabled={confirm.isPending || code.length < 4}>
                    Verify
                  </Button>
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function SecurityTab({ user }: { user: User }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [pw, setPw] = useState({ current_password: "", new_password: "", confirm_password: "" });
  const [del_, setDel] = useState({ password: "", confirm: "" });
  const change = useMutation({
    mutationFn: () => post<{ message: string }>("/api/auth/change-password", pw),
    onSuccess: (r) => {
      toast.success(r.message);
      setPw({ current_password: "", new_password: "", confirm_password: "" });
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => del<{ message: string }>("/api/users/me", del_),
    onSuccess: (r) => {
      queryClient.clear();
      toast.success(r.message);
      router.push("/");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Change password</CardTitle>
          <CardDescription>Other devices will be signed out.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-3">
          <Field label="Current password" htmlFor="cur">
            <Input id="cur" type="password" autoComplete="current-password" value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} />
          </Field>
          <Field label="New password" htmlFor="new">
            <Input id="new" type="password" autoComplete="new-password" value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })} />
          </Field>
          <Field label="Confirm new password" htmlFor="conf">
            <Input id="conf" type="password" autoComplete="new-password" value={pw.confirm_password} onChange={(e) => setPw({ ...pw, confirm_password: e.target.value })} />
          </Field>
          <div className="sm:col-span-3">
            <Button onClick={() => change.mutate()} disabled={change.isPending || !pw.current_password || !pw.new_password}>
              Update password
            </Button>
          </div>
        </CardContent>
      </Card>
      <Card className="border-red-200">
        <CardHeader>
          <CardTitle className="text-destructive">Delete account</CardTitle>
          <CardDescription>
            Permanently deletes {user.email}, every search, opportunity and alert history. This cannot be undone.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-3">
          <Field label="Password" htmlFor="delpw">
            <Input id="delpw" type="password" value={del_.password} onChange={(e) => setDel({ ...del_, password: e.target.value })} />
          </Field>
          <Field label='Type "DELETE" to confirm' htmlFor="delc">
            <Input id="delc" value={del_.confirm} onChange={(e) => setDel({ ...del_, confirm: e.target.value })} />
          </Field>
          <div className="flex items-end">
            <Button variant="destructive" disabled={del_.confirm !== "DELETE" || !del_.password || remove.isPending} onClick={() => remove.mutate()}>
              <Trash2 /> Delete my account
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <AppShell>
      <PageHeader eyebrow="Settings" title="Account & notifications" />
      <RequireAuth>
        {(user) => (
          <Tabs defaultValue="profile">
            <TabsList className="flex-wrap">
              <TabsTrigger value="profile">
                <UserIcon className="size-4" /> Profile
              </TabsTrigger>
              <TabsTrigger value="airports">
                <MapPin className="size-4" /> Airports
              </TabsTrigger>
              <TabsTrigger value="notifications">
                <Bell className="size-4" /> Notifications
              </TabsTrigger>
              <TabsTrigger value="security">
                <KeyRound className="size-4" /> Security
              </TabsTrigger>
            </TabsList>
            <TabsContent value="profile">
              <ProfileTab user={user} />
            </TabsContent>
            <TabsContent value="airports">
              <AirportsTab user={user} />
            </TabsContent>
            <TabsContent value="notifications">
              <NotificationsTab user={user} />
            </TabsContent>
            <TabsContent value="security">
              <SecurityTab user={user} />
            </TabsContent>
          </Tabs>
        )}
      </RequireAuth>
    </AppShell>
  );
}
